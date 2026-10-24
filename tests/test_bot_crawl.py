"""Crawl every inline button reachable from each role's menu and fail on any crash.

The autouse ``_no_swallowed_errors`` fixture turns a logged handler error into a failure.
"""

from __future__ import annotations

import pytest

from app.bot.i18n import t
from app.bot.keyboards.common import ADMIN_MENU_KEYS, BARBER_MENU_KEYS, CLIENT_MENU_KEYS
from app.db.uow import read_tx, write_tx
from app.services import bookings, clock, users
from tests.bot_harness import Harness
from tests.factories import NOW, D, L, World, book

LIMIT = 260


class Clock:
    def __init__(self, mp: pytest.MonkeyPatch, start) -> None:  # type: ignore[no-untyped-def]
        self.now = start
        mp.setattr(clock, "now_utc", lambda: self.now)


async def _seed(world: World) -> None:
    c1 = await world.new_client(name="Birinchi", phone="+998901111111")
    c2 = await world.new_client(name="Ikkinchi")
    await book(world, c1, L(D(0), "10:00"))
    await book(world, c1, L(D(1), "11:00"), service_id="60")
    done = await book(world, c2, L(D(0), "12:00"))
    async with write_tx(world.sm) as s:
        await bookings.complete_booking(
            s, booking_id=done.booking.id, actor_user_id=world.ali_user_id, now=L(D(0), "12:00")
        )
    await world.set("new_client_requires_approval", "true")
    c3 = await world.new_client(name="Uchinchi")
    await book(world, c3, L(D(2), "14:00"))
    async with write_tx(world.sm) as s:
        await bookings.create_manual_booking(
            s,
            actor_user_id=world.ali_user_id,
            barber_id=world.ali_id,
            service_id=world.svc30,
            start_at=L(D(0), "15:00"),
            client_name="Walk In",
            client_phone="901111111",
            now=NOW,
        )


FUZZ = (
    "abc",
    "",
    "0",
    "-5",
    "15",
    "60000",
    "10:00-20:00",
    "28.10",
    "<b>x</b>",
    "Gʻayrat",
    "x" * 400,
)


async def _crawl(H: Harness, tg: int, menu_keys: tuple[str, ...], *, fuzz: bool = False) -> int:  # noqa: N803
    seen: set[str] = set()
    queue: list[str] = []
    taps = 0

    def collect() -> None:
        for c in H.last_callbacks(tg):
            if c and c != "noop" and c not in seen:
                seen.add(c)
                queue.append(c)

    for key in menu_keys:
        await H.text(tg, t("uz", key))
        collect()
    while queue and taps < LIMIT:
        data = queue.pop(0)
        mid = await H.fsm_msg_id(tg) or 5
        await H.tap(tg, data, message_id=mid)
        taps += 1
        collect()
        if fuzz and await H.fsm_state(tg):
            for text in FUZZ:
                if text:
                    await H.text(tg, text)
                collect()
        await H.text(tg, "/cancel")
    return taps


async def test_crawl_admin(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch, NOW)
    await _seed(world)
    H = Harness(world.sm)  # noqa: N806
    taps = await _crawl(H, 1000, ADMIN_MENU_KEYS)
    print("admin taps", taps)
    assert taps > 30


async def test_crawl_barber(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch, L(D(0), "12:30"))
    await _seed(world)
    H = Harness(world.sm)  # noqa: N806
    taps = await _crawl(H, 2000, BARBER_MENU_KEYS)
    print("taps", taps)
    assert taps > 10


async def test_crawl_client(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch, NOW)
    await _seed(world)
    H = Harness(world.sm)  # noqa: N806
    async with read_tx(world.sm) as s:
        found = await users.find_users_by_phone(
            s, actor_user_id=world.admin_id, phone_query="1111111"
        )
    tg = found[0].tg_id
    taps = await _crawl(H, tg, CLIENT_MENU_KEYS)
    print("taps", taps)
    assert taps > 10


@pytest.mark.parametrize("who", ["admin", "barber"])
async def test_crawl_with_text_fuzz(
    world: World, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    Clock(monkeypatch, L(D(0), "12:30"))
    await _seed(world)
    H = Harness(world.sm)  # noqa: N806
    if who == "admin":
        await _crawl(H, 1000, ADMIN_MENU_KEYS, fuzz=True)
    else:
        await _crawl(H, 2000, BARBER_MENU_KEYS, fuzz=True)
