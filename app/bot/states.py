"""FSM state groups (ARCHITECTURE.md §8)."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    lang = State()
    consent = State()


class BookingFlow(StatesGroup):
    service = State()
    barber = State()
    barber_card = State()
    day = State()
    calendar = State()
    slot = State()
    comment = State()
    contact = State()
    name_input = State()
    confirm = State()


class RejectFlow(StatesGroup):
    reason_text = State()  # data: booking_id


class StaffCancelFlow(StatesGroup):
    reason_text = State()  # data: booking_id


class BlockTimeFlow(StatesGroup):
    day = State()
    start = State()
    end = State()
    reason = State()
    confirm_conflicts = State()


class ManualBookingFlow(StatesGroup):
    service = State()
    day = State()
    slot = State()
    name = State()
    phone = State()
    confirm = State()


class AdminBarberFlow(StatesGroup):
    name = State()
    bio = State()
    photo = State()
    edit_field = State()
    deact_reason = State()


class AdminServiceFlow(StatesGroup):
    name = State()
    duration = State()
    price = State()
    edit_field = State()


class AdminHoursFlow(StatesGroup):
    weekday = State()
    start = State()
    end = State()
    break_ = State()


class AdminTimeOffFlow(StatesGroup):
    target = State()
    start_day = State()
    end_day = State()
    hours = State()
    reason = State()
    confirm_conflicts = State()


class AdminSettingsFlow(StatesGroup):
    value = State()  # data: key


class AdminClientFlow(StatesGroup):
    search = State()
    bonus_delta = State()
    bonus_comment = State()


class AdminBookingsFlow(StatesGroup):
    search = State()


class AdminAdminsFlow(StatesGroup):
    promote = State()


class AdminStatsFlow(StatesGroup):
    """Bot-local addition: custom date range input for stats."""

    range = State()
