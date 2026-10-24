"""All enums (ARCHITECTURE.md §3.1). Always use ``.value`` when a plain string is needed."""

from __future__ import annotations

from enum import Enum


class BookingStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    COMPLETED = "COMPLETED"
    NO_SHOW = "NO_SHOW"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED_BY_CLIENT = "CANCELLED_BY_CLIENT"
    CANCELLED_BY_BARBER = "CANCELLED_BY_BARBER"


ACTIVE_STATUSES: frozenset[BookingStatus] = frozenset(
    {BookingStatus.PENDING, BookingStatus.CONFIRMED}
)


class Awaiting(str, Enum):
    """Only meaningful while PENDING."""

    BARBER = "barber"
    CLIENT = "client"


class BookingSource(str, Enum):
    BOT = "bot"
    MANUAL = "manual"


class CancelledBy(str, Enum):
    CLIENT = "client"
    BARBER = "barber"
    ADMIN = "admin"
    SYSTEM = "system"


class LoyaltyMode(str, Enum):
    STAMPS = "stamps"
    CASHBACK = "cashback"


class BonusReason(str, Enum):
    EARN = "earn"
    REDEEM = "redeem"
    WELCOME = "welcome"
    REFERRAL = "referral"
    MANUAL = "manual"
    REVERSAL = "reversal"


class NotificationKind(str, Enum):
    PENDING_REPING = "rping"  # barber + admins re-ping with decision buttons
    REMINDER_24H = "r24"  # client, with Kelaman / Bekor qilish
    REMINDER_2H = "r2"  # client; skipped if client_ack_at is set
    RETURN = "ret"  # "time for a haircut"
    REVIEW = "rev"  # rating request
    MANUAL_VISIT_CONFIRM = "mcf"  # "were you at Ali's today?" for manual bookings
    COMPLETED_THANKS = "thx"  # "thanks for the visit" after an auto-completion, by day
