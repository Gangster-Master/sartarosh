"""Router assembly (ARCHITECTURE.md §9). Order matters: exits first, fallback last."""

from __future__ import annotations

from aiogram import F, Router

from app.bot.handlers import (
    admin_admins,
    admin_barbers,
    admin_bookings,
    admin_clients,
    admin_hours,
    admin_services,
    admin_settings,
    admin_stats,
    admin_timeoff,
    barber_block,
    barber_decide,
    barber_manual,
    barber_queue,
    client_booking,
    client_feedback,
    client_info,
    client_my,
    fallback,
    onboarding,
)

ORDER = (
    onboarding,
    barber_decide,
    barber_queue,
    barber_block,
    barber_manual,
    admin_admins,
    admin_barbers,
    admin_services,
    admin_hours,
    admin_timeoff,
    admin_bookings,
    admin_clients,
    admin_settings,
    admin_stats,
    client_booking,
    client_my,
    client_info,
    client_feedback,
    fallback,
)


def build_root_router() -> Router:
    root = Router(name="root")
    root.message.filter(F.chat.type == "private")
    for module in ORDER:
        r = module.router
        if r.parent_router is not None:  # rebuilt in tests: detach first
            r._parent_router = None  # noqa: SLF001
        root.include_router(r)
    return root
