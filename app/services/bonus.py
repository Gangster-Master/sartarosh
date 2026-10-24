"""Loyalty ledger: stamps | cashback, welcome, referral (ARCHITECTURE.md §4.12).

Every ledger write updates ``users.bonus_balance`` atomically
(``SET bonus_balance = bonus_balance + :d``) [A3].
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ACTIVE_STATUSES, BonusReason, BookingSource, BookingStatus, LoyaltyMode
from app.db.models import BonusTransaction, Booking, User
from app.services import clock
from app.services._common import add_user_counter, load_booking, load_user, require_admin
from app.services.audit import write_audit
from app.services.dto import BonusStatus, BonusTxDTO, RedeemQuote, ShopSettings
from app.services.errors import InputError
from app.services.settings import get_shop_settings


def _mode(s: ShopSettings) -> LoyaltyMode:
    try:
        return LoyaltyMode(s.loyalty_mode)
    except ValueError:
        return LoyaltyMode.STAMPS


async def _balance(session: AsyncSession, user_id: int) -> int:
    return (
        await session.execute(select(User.bonus_balance).where(User.id == user_id))
    ).scalar_one_or_none() or 0


async def _reserved(session: AsyncSession, user_id: int) -> int:
    return (
        await session.execute(
            select(func.coalesce(func.sum(Booking.bonus_cost), 0)).where(
                Booking.client_id == user_id,
                Booking.status.in_(list(ACTIVE_STATUSES)),
            )
        )
    ).scalar_one() or 0


async def _ledger(
    session: AsyncSession,
    *,
    user_id: int,
    delta: int,
    reason: BonusReason,
    unit: LoyaltyMode,
    booking_id: int | None = None,
    earn_day: date | None = None,
    comment: str | None = None,
    created_by_user_id: int | None = None,
) -> None:
    if delta == 0:
        return
    session.add(
        BonusTransaction(
            user_id=user_id,
            booking_id=booking_id,
            delta=delta,
            unit=unit,
            reason=reason,
            earn_day=earn_day,
            comment=comment,
            created_by_user_id=created_by_user_id,
            created_at=clock.now_utc(),
        )
    )
    await session.flush()
    await add_user_counter(session, user_id, "bonus_balance", delta)


async def _has_row(session: AsyncSession, **where: object) -> bool:
    stmt = select(BonusTransaction.id)
    for k, v in where.items():
        stmt = stmt.where(getattr(BonusTransaction, k) == v)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def get_bonus_status(
    session: AsyncSession, user_id: int, *, recent_limit: int = 5
) -> BonusStatus:
    s = await get_shop_settings(session)
    await load_user(session, user_id)
    mode = _mode(s)
    balance = await _balance(session, user_id)
    reserved = await _reserved(session, user_id)
    available = balance - reserved
    required = s.bonus_required_stamps
    if mode == LoyaltyMode.STAMPS:
        can = s.loyalty_enabled and available >= required
    else:
        can = s.loyalty_enabled and available > 0 and s.cashback_max_redeem_percent > 0
    rows = (
        (
            await session.execute(
                select(BonusTransaction)
                .where(BonusTransaction.user_id == user_id)
                .order_by(BonusTransaction.created_at.desc(), BonusTransaction.id.desc())
                .limit(recent_limit)
            )
        )
        .scalars()
        .all()
    )
    return BonusStatus(
        mode=mode,
        enabled=s.loyalty_enabled,
        balance=balance,
        reserved=reserved,
        available=available,
        required=required,
        progress=max(0, min(available, required)),
        can_redeem=bool(can),
        reward_type=s.bonus_reward_type,
        reward_value=s.bonus_reward_value,
        cashback_percent=s.cashback_percent,
        max_redeem_percent=s.cashback_max_redeem_percent,
        recent=[
            BonusTxDTO(
                id=r.id,
                delta=r.delta,
                unit=r.unit,
                reason=r.reason,
                comment=r.comment,
                created_at=r.created_at,
            )
            for r in rows
        ],
    )


async def quote_redeem(session: AsyncSession, *, user_id: int, price_uzs: int) -> RedeemQuote:
    s = await get_shop_settings(session)
    price = max(0, int(price_uzs))
    available = await _balance(session, user_id) - await _reserved(session, user_id)
    if _mode(s) == LoyaltyMode.STAMPS:
        cost = s.bonus_required_stamps
        possible = s.loyalty_enabled and available >= cost
        if s.bonus_reward_type == "free":
            final = 0
        else:
            final = price * (100 - s.bonus_reward_value) // 100
    else:
        cost = max(0, min(available, price * s.cashback_max_redeem_percent // 100))
        possible = s.loyalty_enabled and cost > 0
        final = price - cost
    if not possible:
        return RedeemQuote(possible=False, cost=0, final_price_uzs=price, discount_uzs=0)
    return RedeemQuote(possible=True, cost=cost, final_price_uzs=final, discount_uzs=price - final)


async def _other_completed(session: AsyncSession, user_ids: list[int], booking_id: int) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.client_id.in_(user_ids),
                Booking.status == BookingStatus.COMPLETED,
                Booking.id != booking_id,
            )
        )
    ).scalar_one()


async def _same_person_ids(session: AsyncSession, user: User) -> list[int]:
    """The user plus deleted accounts that had the same verified phone."""
    from app.services.validation import phone_hash

    ids = [user.id]
    h = phone_hash(user.phone) if user.phone and user.phone_verified else None
    if h:
        ids += list(
            (
                await session.execute(
                    select(User.id).where(User.deleted_phone_hash == h, User.id != user.id)
                )
            ).scalars()
        )
    return ids


async def award_for_completion(
    session: AsyncSession, *, booking_id: int
) -> tuple[int, int, int | None]:
    """Internal. Returns (earned, welcome_credited, referral_credited_to)."""
    b = await load_booking(session, booking_id)
    if b.client_id is None or b.status != BookingStatus.COMPLETED:
        return 0, 0, None
    s = await get_shop_settings(session)
    if not s.loyalty_enabled:
        return 0, 0, None
    mode = _mode(s)
    user = await load_user(session, b.client_id)
    if user.deleted_at is not None:
        return 0, 0, None
    earned = 0
    earn_day = clock.to_local(b.start_at).date()
    skip_earn = mode == LoyaltyMode.STAMPS and b.bonus_redeemed
    if (
        not skip_earn
        and not await _has_row(session, user_id=user.id, earn_day=earn_day)
        and not await _has_row(session, user_id=user.id, booking_id=b.id, reason=BonusReason.EARN)
    ):
        amount = 1 if mode == LoyaltyMode.STAMPS else b.final_price_uzs * s.cashback_percent // 100
        if amount > 0:
            await _ledger(
                session,
                user_id=user.id,
                delta=amount,
                reason=BonusReason.EARN,
                unit=mode,
                booking_id=b.id,
                earn_day=earn_day,
            )
            earned = amount

    person_ids = await _same_person_ids(session, user)
    first = await _other_completed(session, person_ids, b.id) == 0

    welcome = 0
    if (
        first
        and s.welcome_bonus > 0
        and (
            await session.execute(
                select(BonusTransaction.id)
                .where(
                    BonusTransaction.user_id.in_(person_ids),
                    BonusTransaction.reason == BonusReason.WELCOME,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        is None
    ):
        await _ledger(
            session,
            user_id=user.id,
            delta=s.welcome_bonus,
            reason=BonusReason.WELCOME,
            unit=mode,
            booking_id=b.id,
        )
        welcome = s.welcome_bonus

    referral_to: int | None = None
    if (
        first
        and s.referral_enabled
        and s.referral_bonus > 0
        and not b.auto_completed
        and b.source == BookingSource.BOT  # walk-ins could be staged by staff
        and user.referrer_id is not None
        and user.referrer_id != user.id
    ):
        ref = await session.get(User, user.referrer_id)
        if (
            ref is not None
            and not ref.is_blocked
            and ref.deleted_at is None
            and not (ref.phone and user.phone and ref.phone == user.phone)
            and not await _has_row(
                session, user_id=ref.id, booking_id=b.id, reason=BonusReason.REFERRAL
            )
        ):
            # at most one referral credit per invited friend
            already = (
                await session.execute(
                    select(BonusTransaction.id)
                    .join(Booking, Booking.id == BonusTransaction.booking_id)
                    .where(
                        BonusTransaction.user_id == ref.id,
                        BonusTransaction.reason == BonusReason.REFERRAL,
                        Booking.client_id.in_(person_ids),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if already is None:
                await _ledger(
                    session,
                    user_id=ref.id,
                    delta=s.referral_bonus,
                    reason=BonusReason.REFERRAL,
                    unit=mode,
                    booking_id=b.id,
                )
                referral_to = ref.id
    return earned, welcome, referral_to


async def apply_redeem(session: AsyncSession, *, booking_id: int) -> bool:
    """Internal: write -bonus_cost REDEEM. False if the balance is short."""
    b = await load_booking(session, booking_id)
    if b.bonus_cost <= 0:
        return True
    if b.client_id is None:
        return False
    if await _has_row(session, user_id=b.client_id, booking_id=b.id, reason=BonusReason.REDEEM):
        return True
    if await _balance(session, b.client_id) < b.bonus_cost:
        return False
    s = await get_shop_settings(session)
    await _ledger(
        session,
        user_id=b.client_id,
        delta=-b.bonus_cost,
        reason=BonusReason.REDEEM,
        unit=_mode(s),
        booking_id=b.id,
    )
    return True


async def reverse_for_booking(session: AsyncSession, *, booking_id: int) -> int:
    """Internal: negate EARN/WELCOME/REFERRAL and restore REDEEM rows of the booking.

    One REVERSAL row per affected user; balances are clamped at 0. Returns the
    amount taken back from the client (>= 0).
    """
    b = await load_booking(session, booking_id)
    rows = (
        (
            await session.execute(
                select(BonusTransaction).where(
                    BonusTransaction.booking_id == booking_id,
                    BonusTransaction.reason.in_(
                        [
                            BonusReason.EARN,
                            BonusReason.WELCOME,
                            BonusReason.REFERRAL,
                            BonusReason.REDEEM,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    per_user: dict[int, int] = {}
    for r in rows:
        per_user[r.user_id] = per_user.get(r.user_id, 0) - r.delta
    s = await get_shop_settings(session)
    taken_from_client = 0
    for uid, delta in per_user.items():
        if delta == 0:
            continue
        if await _has_row(session, user_id=uid, booking_id=booking_id, reason=BonusReason.REVERSAL):
            continue
        bal = await _balance(session, uid)
        if bal + delta < 0:
            delta = -bal
        if delta == 0:
            continue
        await _ledger(
            session,
            user_id=uid,
            delta=delta,
            reason=BonusReason.REVERSAL,
            unit=_mode(s),
            booking_id=booking_id,
            comment="no_show",
        )
        if uid == b.client_id and delta < 0:
            taken_from_client = -delta
    if b.bonus_redeemed:
        b.bonus_redeemed = False
        await session.flush()
    return taken_from_client


async def adjust_bonus(
    session: AsyncSession, *, actor_user_id: int, user_id: int, delta: int, comment: str
) -> BonusStatus:
    await require_admin(session, actor_user_id)
    comment = (comment or "").strip()
    if not comment:
        raise InputError("comment required", field="comment")
    if len(comment) > 200:
        raise InputError("comment too long", field="comment", limit=200)
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        raise InputError("invalid delta", field="delta") from None
    if delta == 0:
        raise InputError("delta must not be 0", field="delta")
    if abs(delta) > 1_000_000_000:
        raise InputError("delta too large", field="delta")
    await load_user(session, user_id)
    if await _balance(session, user_id) + delta < 0:
        raise InputError("balance may not go below 0", field="delta")
    s = await get_shop_settings(session)
    await _ledger(
        session,
        user_id=user_id,
        delta=delta,
        reason=BonusReason.MANUAL,
        unit=_mode(s),
        comment=comment,
        created_by_user_id=actor_user_id,
    )
    await write_audit(
        session,
        actor_user_id=actor_user_id,
        action="bonus.adjust",
        entity="user",
        entity_id=user_id,
        payload={"delta": delta, "comment": comment},
    )
    return await get_bonus_status(session, user_id)


async def recompute_balance(session: AsyncSession, user_id: int) -> int:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(BonusTransaction.delta), 0)).where(
                BonusTransaction.user_id == user_id
            )
        )
    ).scalar_one() or 0
    u = await load_user(session, user_id)
    u.bonus_balance = int(total)
    await session.flush()
    return int(total)
