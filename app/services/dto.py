"""Data transfer objects returned by services (ARCHITECTURE.md §4.2).

All datetimes are aware UTC. A ``date`` is a local calendar day in SHOP_TZ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Generic, TypeVar

from app.db.enums import (
    Awaiting,
    BonusReason,
    BookingSource,
    BookingStatus,
    CancelledBy,
    LoyaltyMode,
    NotificationKind,
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Roles:
    is_client: bool
    is_barber: bool
    is_admin: bool
    is_env_admin: bool
    barber_id: int | None


@dataclass(frozen=True, slots=True)
class UserDTO:
    id: int
    tg_id: int
    tg_username: str | None
    first_name: str
    full_name: str | None
    display_name: str  # full_name or first_name
    phone: str | None
    phone_verified: bool
    lang: str
    lang_chosen: bool
    consent_version: int | None
    is_admin: bool  # promoted-in-DB flag only
    is_blocked: bool
    no_show_count: int
    late_cancel_count: int
    bonus_balance: int
    ref_code: str
    referrer_id: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UserContext:
    user: UserDTO
    roles: Roles
    barber_reachable: bool | None


@dataclass(frozen=True, slots=True)
class BarberDTO:
    id: int
    user_id: int | None
    tg_id: int | None
    display_name: str
    photo_file_id: str | None
    bio: str | None
    is_active: bool
    is_public: bool
    sort_order: int
    slot_step_min: int
    buffer_min: int
    auto_accept: bool
    is_reachable: bool
    invite_token: str | None
    invite_expires_at: datetime | None
    pending_link_user_id: int | None


@dataclass(frozen=True, slots=True)
class LinkRequest:
    barber: BarberDTO
    user: UserDTO


@dataclass(frozen=True, slots=True)
class ServiceDTO:
    id: int
    name: str
    duration_min: int
    price_uzs: int
    is_active: bool
    sort_order: int


@dataclass(frozen=True, slots=True)
class ResolvedService:
    service_id: int | None
    name: str | None
    duration_min: int
    price_uzs: int


@dataclass(frozen=True, slots=True)
class WorkingHoursDTO:
    barber_id: int
    weekday: int
    start_time: time
    end_time: time
    break_start: time | None
    break_end: time | None


@dataclass(frozen=True, slots=True)
class ShopDayHours:
    weekday: int
    open_time: time | None
    close_time: time | None


@dataclass(frozen=True, slots=True)
class TimeOffDTO:
    id: int
    barber_id: int | None
    start_at: datetime
    end_at: datetime
    reason: str | None


@dataclass(frozen=True, slots=True)
class Interval:
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class DayAvailability:
    day: date
    is_working: bool
    has_free_slots: bool


@dataclass(frozen=True, slots=True)
class BookingDTO:
    id: int
    status: BookingStatus
    awaiting: Awaiting | None
    source: BookingSource
    client_id: int | None
    client_tg_id: int | None
    client_lang: str
    client_name: str
    client_phone: str | None  # masked for barbers on old bookings
    client_phone_verified: bool
    barber_id: int
    barber_name: str
    barber_tg_id: int | None
    service_id: int | None
    service_name: str | None
    start_at: datetime
    end_at: datetime
    duration_min: int
    original_start_at: datetime | None
    proposal_count: int
    price_snapshot: int
    final_price_uzs: int
    bonus_cost: int
    bonus_redeemed: bool
    comment: str | None
    expire_at: datetime | None
    late_cancel: bool
    client_ack_at: datetime | None
    auto_completed: bool
    completed_at: datetime | None
    visit_confirmed: bool | None
    cancelled_by: CancelledBy | None
    cancel_reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DecisionTarget:
    chat_id: int
    lang: str
    role: str  # "barber" | "admin"


@dataclass(frozen=True, slots=True)
class BookingCreated:
    booking: BookingDTO
    auto_confirmed: bool
    client_soft_blocked: bool
    decision_targets: list[DecisionTarget]


@dataclass(frozen=True, slots=True)
class CancelResult:
    booking: BookingDTO
    late: bool


@dataclass(frozen=True, slots=True)
class CompleteResult:
    booking: BookingDTO
    earned: int
    redeemed: bool
    bonus_failed: bool
    new_balance: int
    welcome_credited: int
    referral_credited_to: int | None
    needs_visit_confirm: bool


@dataclass(frozen=True, slots=True)
class NoShowResult:
    booking: BookingDTO
    reversed: int


@dataclass(frozen=True, slots=True)
class ProposalResult:
    booking: BookingDTO


@dataclass(frozen=True, slots=True)
class ClientBookings:
    upcoming: list[BookingDTO]
    past: list[BookingDTO]


@dataclass(frozen=True, slots=True)
class ClientStats:
    total_completed: int
    no_show_count: int
    late_cancel_count: int
    is_new: bool


@dataclass(frozen=True, slots=True)
class RebookHint:
    barber_id: int
    barber_name: str
    service_id: int | None
    service_name: str | None


@dataclass(frozen=True, slots=True)
class BonusTxDTO:
    id: int
    delta: int
    unit: LoyaltyMode
    reason: BonusReason
    comment: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BonusStatus:
    mode: LoyaltyMode
    enabled: bool
    balance: int
    reserved: int
    available: int
    required: int
    progress: int
    can_redeem: bool
    reward_type: str
    reward_value: int
    cashback_percent: int
    max_redeem_percent: int
    recent: list[BonusTxDTO]


@dataclass(frozen=True, slots=True)
class RedeemQuote:
    possible: bool
    cost: int
    final_price_uzs: int
    discount_uzs: int


@dataclass(frozen=True, slots=True)
class ContactResult:
    user: UserDTO
    is_foreign: bool
    duplicate_user_ids: list[int]


@dataclass(frozen=True, slots=True)
class TimeOffResult:
    time_off: TimeOffDTO
    cancelled: list[BookingDTO]
    overlapping_kept: list[BookingDTO]


@dataclass(frozen=True, slots=True)
class HoursChangeResult:
    hours: WorkingHoursDTO | None
    out_of_hours: list[BookingDTO]


@dataclass(frozen=True, slots=True)
class DueNotification:
    id: int
    kind: NotificationKind
    booking: BookingDTO
    targets: list[DecisionTarget]


@dataclass(frozen=True, slots=True)
class PendingDigestItem:
    booking: BookingDTO
    targets: list[DecisionTarget]


@dataclass(frozen=True, slots=True)
class ClientProfile:
    user: UserDTO
    stats: ClientStats
    recent_bookings: list[BookingDTO]
    bonus: BonusStatus
    soft_blocked: bool


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: list[T]
    page: int  # 0-based
    pages: int
    total: int


@dataclass(frozen=True, slots=True)
class ReviewResult:
    booking_id: int
    rating: int
    is_low: bool  # rating <= 3


@dataclass(frozen=True, slots=True)
class BarberStatsRow:
    barber_id: int
    barber_name: str
    total: int
    completed: int
    auto_completed: int
    manual_completed: int
    no_show: int
    cancelled: int
    rejected: int
    expired: int
    revenue_uzs: int


@dataclass(frozen=True, slots=True)
class ServiceStatsRow:
    service_name: str
    completed: int
    revenue_uzs: int


@dataclass(frozen=True, slots=True)
class StatsDTO:
    start: date
    end: date
    rows: list[BarberStatsRow]
    totals: BarberStatsRow
    top_services: list[ServiceStatsRow]
    new_clients: int


@dataclass(frozen=True, slots=True)
class ShopSettings:
    """One typed field per key of ``settings.DEFAULTS`` (same names).

    Job times (``daily_summary_time`` etc.) are parsed into ``datetime.time``.
    """

    # booking
    booking_horizon_days: int = 7
    min_lead_min: int = 30
    free_cancel_hours: int = 2
    max_active_bookings: int = 3
    max_requests_per_day: int = 5
    # pending / decision
    pending_timeout_min: int = 60
    pending_cutoff_before_start_min: int = 10
    min_decision_min: int = 15
    pending_reping_min: int = 15
    proposal_reply_min: int = 30
    late_accept_grace_min: int = 10
    new_client_requires_approval: bool = True
    # reminders / retention
    reminder_24h_enabled: bool = True
    reminder_2h_enabled: bool = True
    return_reminder_weeks: int = 4
    reviews_enabled: bool = True
    review_delay_hours: int = 2
    # no-show policy
    late_cancels_per_no_show: int = 0
    soft_block_no_shows: int = 3
    soft_block_window_days: int = 90
    auto_complete_after_hours: int = 12
    noshow_flip_window_hours: int = 24
    # loyalty
    loyalty_enabled: bool = True
    loyalty_mode: str = "stamps"
    bonus_required_stamps: int = 5
    bonus_reward_type: str = "free"
    bonus_reward_value: int = 100
    cashback_percent: int = 5
    cashback_max_redeem_percent: int = 50
    welcome_bonus: int = 1
    referral_enabled: bool = True
    referral_bonus: int = 1
    # staff
    barbers_can_block_time: bool = True
    allow_foreign_phone: bool = True
    # jobs (local time)
    daily_summary_time: time = time(8, 0)
    nightly_check_time: time = time(23, 0)
    backup_time: time = time(3, 30)
    # shop info
    shop_name: str = ""
    address: str = ""
    latitude: float | None = None
    longitude: float | None = None
    contact_phone: str = ""
    map_yandex_url: str = ""
    map_google_url: str = ""
    map_2gis_url: str = ""
