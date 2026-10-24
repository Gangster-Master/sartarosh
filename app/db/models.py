"""All ORM models (ARCHITECTURE.md §3.2).

Only BACKEND code imports this module. Bot code works with DTOs.
Relationships are intentionally not declared: services load related rows
explicitly, which keeps async code free of implicit lazy loads.
"""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, TimestampMixin, utcnow
from app.db.enums import (
    Awaiting,
    BonusReason,
    BookingSource,
    BookingStatus,
    CancelledBy,
    LoyaltyMode,
    NotificationKind,
)
from app.db.types import StrEnumType, UTCDateTime


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    tg_username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    full_name: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str | None] = mapped_column(String(16), index=True)
    phone_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lang: Mapped[str] = mapped_column(String(5), nullable=False, default="uz")
    lang_chosen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_version: Mapped[int | None] = mapped_column(Integer)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    no_show_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    late_cancel_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_show_reset_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    bonus_balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ref_code: Mapped[str] = mapped_column(String(12), nullable=False, unique=True)
    referrer_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    acq_source: Mapped[str | None] = mapped_column(String(32))
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # sha256 of the verified phone, kept after /delete_me so one-time rewards
    # (welcome, referral) cannot be collected again by re-registering.
    deleted_phone_hash: Mapped[str | None] = mapped_column(String(64), index=True)


class Barber(TimestampMixin, Base):
    __tablename__ = "barbers"
    __table_args__ = (
        CheckConstraint("slot_step_min IN (15, 30, 45, 60)", name="slot_step"),
        CheckConstraint("buffer_min IN (0, 15, 30)", name="buffer"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), unique=True
    )
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    photo_file_id: Mapped[str | None] = mapped_column(String(256))
    bio: Mapped[str | None] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    slot_step_min: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    buffer_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    auto_accept: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invite_token: Mapped[str | None] = mapped_column(String(32), unique=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    pending_link_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    is_reachable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Service(TimestampMixin, Base):
    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint(
            "duration_min > 0 AND duration_min % 15 = 0 AND duration_min <= 240",
            name="duration",
        ),
        CheckConstraint("price_uzs >= 0", name="price"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    price_uzs: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class BarberService(Base):
    __tablename__ = "barber_services"
    __table_args__ = (
        PrimaryKeyConstraint("barber_id", "service_id"),
        CheckConstraint("price_uzs IS NULL OR price_uzs >= 0", name="price"),
        CheckConstraint(
            "duration_min IS NULL OR (duration_min > 0 AND duration_min % 15 = 0 "
            "AND duration_min <= 240)",
            name="duration",
        ),
    )

    barber_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("barbers.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    price_uzs: Mapped[int | None] = mapped_column(Integer)
    duration_min: Mapped[int | None] = mapped_column(Integer)


class WorkingHours(Base):
    __tablename__ = "working_hours"
    __table_args__ = (
        UniqueConstraint("barber_id", "weekday"),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="weekday"),
        CheckConstraint("start_time < end_time", name="start_end"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    barber_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("barbers.id", ondelete="CASCADE"), nullable=False
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    break_start: Mapped[time | None] = mapped_column(Time)
    break_end: Mapped[time | None] = mapped_column(Time)


class TimeOff(CreatedMixin, Base):
    __tablename__ = "time_off"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="end_after_start"),
        Index("ix_time_off_barber_id_start_at", "barber_id", "start_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    barber_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("barbers.id", ondelete="CASCADE")
    )
    start_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )


class Booking(TimestampMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="end_after_start"),
        CheckConstraint("proposal_count <= 2", name="proposal_count"),
        Index("ix_bookings_barber_id_start_at", "barber_id", "start_at"),
        Index("ix_bookings_client_id_start_at", "client_id", "start_at"),
        Index("ix_bookings_client_phone", "client_phone"),
        Index("ix_bookings_status_expire_at", "status", "expire_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    client_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    barber_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("barbers.id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="SET NULL")
    )
    service_name_snapshot: Mapped[str | None] = mapped_column(String(64))
    start_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    block_end_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    price_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    bonus_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    final_price_uzs: Mapped[int] = mapped_column(Integer, nullable=False)
    bonus_redeemed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[BookingStatus] = mapped_column(
        StrEnumType(BookingStatus, 20), nullable=False, index=True
    )
    awaiting: Mapped[Awaiting | None] = mapped_column(StrEnumType(Awaiting, 8))
    proposal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    original_start_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source: Mapped[BookingSource] = mapped_column(StrEnumType(BookingSource, 8), nullable=False)
    client_name: Mapped[str] = mapped_column(String(64), nullable=False)
    client_phone: Mapped[str | None] = mapped_column(String(16))
    client_phone_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    comment: Mapped[str | None] = mapped_column(String(300))
    expire_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    late_cancel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    client_ack_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    auto_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    visit_confirmed: Mapped[bool | None] = mapped_column(Boolean)
    cancelled_by: Mapped[CancelledBy | None] = mapped_column(StrEnumType(CancelledBy, 8))
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )


class BookingSlot(Base):
    """DB-level double-booking guard: one row per 15-minute cell of an ACTIVE booking."""

    __tablename__ = "booking_slots"
    __table_args__ = (PrimaryKeyConstraint("barber_id", "slot_start"),)

    barber_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("barbers.id", ondelete="CASCADE"), nullable=False
    )
    slot_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    booking_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )


class BookingMessage(CreatedMixin, Base):
    __tablename__ = "booking_messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(8), nullable=False)


class ScheduledNotification(Base):
    __tablename__ = "scheduled_notifications"
    __table_args__ = (UniqueConstraint("booking_id", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[NotificationKind] = mapped_column(StrEnumType(NotificationKind, 8), nullable=False)
    send_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200))


class BonusTransaction(CreatedMixin, Base):
    __tablename__ = "bonus_transactions"
    __table_args__ = (
        CheckConstraint("delta <> 0", name="delta_nonzero"),
        UniqueConstraint("user_id", "booking_id", "reason"),
        UniqueConstraint("user_id", "earn_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    booking_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bookings.id", ondelete="SET NULL")
    )
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[LoyaltyMode] = mapped_column(StrEnumType(LoyaltyMode, 8), nullable=False)
    reason: Mapped[BonusReason] = mapped_column(StrEnumType(BonusReason, 10), nullable=False)
    earn_day: Mapped[date | None] = mapped_column(Date)
    comment: Mapped[str | None] = mapped_column(String(200))
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )


class Review(CreatedMixin, Base):
    __tablename__ = "reviews"
    __table_args__ = (CheckConstraint("rating >= 1 AND rating <= 5", name="rating"),)

    booking_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bookings.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    barber_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("barbers.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)


class FsmState(Base):
    __tablename__ = "fsm_states"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(128))
    data: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )


class AuditLog(CreatedMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "AuditLog",
    "Barber",
    "BarberService",
    "Base",
    "BonusTransaction",
    "Booking",
    "BookingMessage",
    "BookingSlot",
    "FsmState",
    "Review",
    "ScheduledNotification",
    "Service",
    "Setting",
    "TimeOff",
    "User",
    "WorkingHours",
]
