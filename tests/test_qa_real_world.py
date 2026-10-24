"""QA regression (real_world): a Tashkent barbershop running the bot for a month.

Each test asserts the correct behaviour; each was a confirmed bug, now fixed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod

from app.bot.callbacks import DelMeCb
from app.bot.i18n import t
from app.bot.scheduler import Ticker
from app.db.enums import Awaiting, BookingStatus
from app.db.uow import read_tx, write_tx
from app.services import bookings, clock, users
from app.services.errors import ServiceError
from tests.bot_harness import FakeSession, Harness
from tests.factories import NOW, D, L, World, book


class Clock:
    def __init__(self, mp: pytest.MonkeyPatch, start) -> None:  # type: ignore[no-untyped-def]
        self.now = start
        mp.setattr(clock, "now_utc", lambda: self.now)


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


@pytest.fixture
def H(world: World, clk: Clock) -> Harness:  # noqa: N802
    return Harness(world.sm)


async def _tg(w: World, uid: int) -> int:
    async with read_tx(w.sm) as s:
        return (await users.get_user(s, uid)).tg_id


async def _get(w: World, bid: int) -> Any:
    async with read_tx(w.sm) as s:
        return await bookings.get_booking(s, bid)


# ------------------------------------------------------------------ 1. 03:30 push


async def test_auto_completion_push_is_not_sent_in_the_middle_of_the_night(
    world: World,
    H: Harness,
    clk: Clock,  # noqa: N803
) -> None:
    """auto_complete_after_hours=12 means a normal 15:00 visit is auto-completed at 03:30
    local. The tick sends the client "Tashrifingiz uchun rahmat! + stamp" right then.
    RETURN / REVIEW / MANUAL_VISIT_CONFIRM are clamped to 10:00-21:00, this push is not."""
    c = await world.new_client()
    tg = await _tg(world, c)
    await book(world, c, L(D(0), "15:00"))
    clk.now = L(D(1), "03:30")  # end 15:30 + 12 h
    await Ticker(H.bot, world.sm, H.notifier).tick()
    night = H.texts_to(tg)
    assert night == [], f"client got a push at 03:30 local: {night!r}"


# ------------------------------------------------------------------ 2. barber /delete_me


async def test_barber_delete_me_does_not_silently_orphan_client_bookings(
    world: World,
    H: Harness,  # noqa: N803
) -> None:
    """A barber who quits runs /delete_me. His profile is unlinked (hidden from clients),
    but his clients' CONFIRMED bookings stay as they were and nobody is told: the
    admins get no alert and the clients still get "Ali sizni kutadi" reminders."""
    c = await world.new_client()
    r = await book(world, c, L(D(2), "11:00"))
    assert r.booking.status == BookingStatus.CONFIRMED
    await H.text(2000, "/delete_me")
    await H.tap(2000, DelMeCb(a="yes").pack())
    async with read_tx(world.sm) as s:
        from app.services import barbers as _barbers

        assert (await _barbers.get_barber(s, world.ali_id)).user_id is None  # really deleted
    b = await _get(world, r.booking.id)
    admin_told = len(H.sent_to(1000)) > 0
    client_told = b.status != BookingStatus.CONFIRMED
    assert admin_told or client_told, (
        "barber deleted himself; booking still CONFIRMED and no admin was alerted"
    )


# ------------------------------------------------------------------ 3. late accept of a ghost


async def test_late_accept_refused_after_client_deleted_account(world: World) -> None:
    """The barber ignores a request, it expires, the client gives up and runs /delete_me.
    Inside the 10-min grace the barber taps the old ✅: the booking becomes CONFIRMED for a
    deleted account (name "—", no phone) and the slot is blocked for nobody."""
    bek, bek_user = await world.new_barber("Bekzod", auto_accept=False)
    c = await world.new_client(name="Mijoz")
    r = await book(world, c, L(D(0), "15:00"), barber_id=bek)
    assert r.booking.status == BookingStatus.PENDING
    exp = r.booking.expire_at
    async with write_tx(world.sm) as s:
        await bookings.expire_due_pending(s, now=exp)
    async with write_tx(world.sm) as s:
        await users.delete_me(s, user_id=c, now=exp + timedelta(minutes=2))
    try:
        async with write_tx(world.sm) as s:
            await bookings.confirm_booking(
                s,
                booking_id=r.booking.id,
                actor_user_id=bek_user,
                now=exp + timedelta(minutes=5),
            )
    except ServiceError:
        pass
    b = await _get(world, r.booking.id)
    assert b.status != BookingStatus.CONFIRMED, "deleted client's expired request was accepted"


async def test_late_accept_refused_after_client_blocked(world: World) -> None:
    """Same as above with an admin blocking the client (prank requests): set_blocked only
    cancels ACTIVE bookings, the EXPIRED one can still be late-accepted."""
    bek, bek_user = await world.new_barber("Bekzod", auto_accept=False)
    c = await world.new_client(name="Mijoz")
    r = await book(world, c, L(D(0), "15:00"), barber_id=bek)
    exp = r.booking.expire_at
    async with write_tx(world.sm) as s:
        await bookings.expire_due_pending(s, now=exp)
    async with write_tx(world.sm) as s:
        await users.set_blocked(
            s, actor_user_id=world.admin_id, user_id=c, blocked=True, now=exp + timedelta(minutes=1)
        )
    try:
        async with write_tx(world.sm) as s:
            await bookings.confirm_booking(
                s,
                booking_id=r.booking.id,
                actor_user_id=bek_user,
                now=exp + timedelta(minutes=5),
            )
    except ServiceError:
        pass
    b = await _get(world, r.booking.id)
    assert b.status != BookingStatus.CONFIRMED, "blocked client's expired request was accepted"


# ------------------------------------------------------------------ 4. lost request


class _FlakySession(FakeSession):
    """Telegram unreachable while ``down`` is True (network blip / API outage)."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if self.down and type(method).__name__.startswith("Send"):
            self.calls.append(method)
            raise TelegramNetworkError(method=method, message="Request timeout error")
        return await super().make_request(bot, method, timeout)


async def test_request_is_not_lost_when_telegram_is_down_at_booking_time(
    world: World, clk: Clock
) -> None:
    """A client books at 22:00 (outside working hours) for Thursday. The decision message
    send fails with a network error. Nothing retries it: no reping (created outside the
    window), the morning digest only covers *today's* requests, so neither the barber nor an
    admin ever sees Accept/Reject and the request silently expires."""
    h = Harness(world.sm)
    flaky = _FlakySession()
    h.session = flaky
    h.bot.session = flaky
    bek, bek_user = await world.new_barber("Bekzod", auto_accept=False)
    async with read_tx(world.sm) as s:
        bek_tg = (await users.get_user(s, bek_user)).tg_id
    c = await world.new_client(name="Mijoz")
    clk.now = L(D(0), "22:00")
    r = await book(world, c, L(D(3), "12:00"), barber_id=bek, now=clk.now)
    assert r.booking.status == BookingStatus.PENDING and r.booking.awaiting == Awaiting.BARBER
    flaky.down = True
    await h.notifier.booking_created(r)
    flaky.down = False
    flaky.calls.clear()
    ticker = Ticker(h.bot, world.sm, h.notifier)
    deadline = r.booking.expire_at
    while clk.now < deadline:
        clk.now += timedelta(minutes=30)
        await ticker.tick()
    decision_header = t("uz", "notify.decision_new").split("\n")[0][:10]
    got = [
        x
        for chat in (bek_tg, 1000)
        for x in h.texts_to(chat)
        if f"#{r.booking.id}" in x or "Mijoz" in x
    ]
    assert any("Mijoz" in x and "⌛" not in x for x in got), (
        f"nobody was ever asked to decide (header {decision_header!r}); got {got!r}"
    )


# ------------------------------------------------------------------ 5. update while running


def test_refused_second_instance_does_not_migrate_the_live_database(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """README "Update → Local / systemd" says: git pull && pip install && run.py --migrate,
    while the systemd service is still running. run.py applies the migrations BEFORE taking
    bot.lock, so the second process migrates the database under the running old code and only
    then exits 3 ("already running"); the service keeps running the old code."""
    import fcntl
    import os
    import shutil
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    from app.db import migrate as _migrate

    src = Path(_migrate.__file__).resolve().parents[2]
    root = tmp_path / "proj"
    root.mkdir()
    shutil.copytree(src / "app", root / "app", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(
        src / "migrations", root / "migrations", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy(src / "run.py", root / "run.py")
    shutil.copy(src / "alembic.ini", root / "alembic.ini")
    (root / ".env").write_text("BOT_TOKEN=0:fake\nADMIN_IDS=1\n")
    data = root / "data"
    data.mkdir()
    db = data / "bot.db"
    # the running (old) version is at 0001
    from alembic import command

    command.upgrade(_migrate.alembic_config(f"sqlite+aiosqlite:///{db}"), "0001")
    # the running instance holds the lock
    fh = open(data / "bot.lock", "a+")  # noqa: SIM115
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("DATABASE_URL", "BOT_TOKEN", "ADMIN_IDS", "DATA_DIR", "REDIS_URL")
        }
        env.update(
            PYTHONPATH=str(root),
            DATABASE_URL=f"sqlite+aiosqlite:///{db}",
            DATA_DIR=str(data),
            BOT_TOKEN="0:fake",
            WEBAPP_ENABLED="false",
        )
        p = subprocess.run(
            [sys.executable, "run.py", "--migrate"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    assert p.returncode == 3, p.stdout + p.stderr
    con = sqlite3.connect(db)
    try:
        rev = con.execute("select version_num from alembic_version").fetchone()[0]
    finally:
        con.close()
    assert rev == "0001", f"a refused second instance migrated the live DB to {rev}"


# ------------------------------------------------------------------ 6. digests


async def test_nightly_digest_of_deactivated_barber_goes_to_admins(
    world: World,
    H: Harness,
    clk: Clock,  # noqa: N803
) -> None:
    """Ali works the morning, the admin deactivates him at 11:45 (he quit). His 11:30 visit
    is unresolved at 23:00. The digest with ✅/🚫 buttons goes to Ali, whose taps are refused
    (IsStaff needs an active barber), and the admins never see it; the visit then
    auto-completes and the client gets a stamp whether he came or not."""
    from app.services import barbers as _barbers

    c = await world.new_client(name="Mijoz")
    await book(world, c, L(D(0), "11:30"))
    async with write_tx(world.sm) as s:
        await _barbers.deactivate_barber(
            s,
            actor_user_id=world.admin_id,
            barber_id=world.ali_id,
            reason="ketdi",
            now=L(D(0), "11:45"),
        )
    clk.now = L(D(0), "23:05")
    await Ticker(H.bot, world.sm, H.notifier).tick()
    to_admin = [x for x in H.texts_to(1000) if "Mijoz" in x]
    assert to_admin, f"admins never got the unresolved visit; Ali got: {H.texts_to(2000)!r}"


async def test_morning_summary_does_not_count_unanswered_requests_as_bookings(
    world: World,
    H: Harness,
    clk: Clock,  # noqa: N803
) -> None:
    """The 08:00 "Bugungi navbatlar: N ta" list is built from ACTIVE bookings, so a PENDING
    request the barber has not accepted (and may let expire) is listed exactly like a
    confirmed client, with no marker."""
    c1 = await world.new_client(name="Tasdiq")
    await book(world, c1, L(D(1), "10:00"), now=NOW)  # confirmed (auto-accept, not new)
    await world.set("new_client_requires_approval", "true")
    c2 = await world.new_client(name="Kutayotgan")
    r2 = await book(world, c2, L(D(1), "16:00"), now=L(D(0), "21:00"))  # sent after hours
    assert r2.booking.status == BookingStatus.PENDING
    clk.now = L(D(1), "08:01")
    await Ticker(H.bot, world.sm, H.notifier).tick()
    summary = next(
        x
        for x in H.texts_to(2000)
        if x.startswith(t("uz", "notify.morning", n="").split("{")[0][:12])
    )
    line = (
        next(x for x in summary.split("\n") if "Kutayotgan" in x) if "Kutayotgan" in summary else ""
    )
    assert t("uz", "notify.morning", n=1) in summary or "⏳" in line, summary


async def test_morning_visits_reach_the_nightly_digest_before_auto_completion(
    world: World,
    H: Harness,
    clk: Clock,  # noqa: N803
) -> None:
    """Defaults: nightly_check_time 23:00, auto_complete_after_hours 12. A 10:00-10:30 visit
    (the first slot of a 10:00 shop) is auto-completed at 22:30, so it never appears in the
    23:00 "please mark done / no-show" digest. The barber is never asked; a no-show client
    gets a stamp + welcome bonus and the "Rahmat" push."""
    c = await world.new_client(name="Ertalabki")
    await book(world, c, L(D(0), "10:00"))
    ticker = Ticker(H.bot, world.sm, H.notifier)
    for hhmm in ("12:00", "18:00", "22:31", "23:01"):
        clk.now = L(D(0), hhmm)
        await ticker.tick()
    asked = [x for x in H.texts_to(2000) if "Ertalabki" in x]
    assert asked, "the barber was never asked whether the 10:00 client came"
