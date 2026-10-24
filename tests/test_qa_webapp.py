"""QA regression (webapp hunter): Mini App API + static frontend defects.

Clock pinned at NOW = Mon 2026-10-26 09:00 Tashkent; Ali works Mon–Sat 10–20.
Each test asserts the correct behaviour; each was a confirmed bug, now fixed.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from app.webapp import server
from app.webapp.serializers import iso
from app.webapp.tasks import drain_pending_notifications
from tests.factories import MONDAY, NOW, L, World
from tests.webapp_harness import Api, Clock, make_api, tg_of

STATIC = Path(server.STATIC_DIR)


@pytest.fixture
def clk(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch, NOW)


@pytest_asyncio.fixture
async def api(world: World, clk: Clock) -> AsyncIterator[Api]:
    a = await make_api(world.sm)
    yield a
    await drain_pending_notifications()
    await a.client.close()


async def _client(w: World) -> tuple[int, int]:
    uid = await w.new_client()
    return uid, await tg_of(w.sm, uid)


# ----------------------------------------------------------------- frontend module graph

_IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}\s*from\s*'(\.[^']+)'", re.S)
_EXPORT_RE = re.compile(
    r"export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)"
)
_EXPORT_LIST_RE = re.compile(r"export\s*\{([^}]*)\}")


def _exports(path: Path) -> set[str]:
    src = path.read_text(encoding="utf-8")
    names = set(_EXPORT_RE.findall(src))
    for group in _EXPORT_LIST_RE.findall(src):
        for part in group.split(","):
            part = part.strip()
            if part:
                names.add(part.split(" as ")[-1].strip())
    return names


def test_every_named_import_in_static_js_is_exported() -> None:
    """An ES module that imports a name its dependency does not export fails to LINK:
    the whole screen module (and the booking wizard) never loads in the browser."""
    missing: list[str] = []
    for js in sorted(STATIC.rglob("*.js")):
        src = js.read_text(encoding="utf-8")
        for names, rel in _IMPORT_RE.findall(src):
            dep = (js.parent / rel).resolve()
            assert dep.is_file(), f"{js.name}: import of missing file {rel}"
            have = _exports(dep)
            for n in names.split(","):
                n = n.strip().split(" as ")[0].strip()
                if n and n not in have:
                    missing.append(f"{js.relative_to(STATIC)} imports {n!r} from {rel}")
    assert not missing, "\n".join(missing)


# ----------------------------------------------------------------- crafted API calls


async def test_booking_without_service_is_refused_when_shop_has_services(
    world: World, api: Api
) -> None:
    """The bot (and the app UI) only skip the service step when the shop has NO services.
    A crafted POST with service_id=null still creates a 0 so'm, service-less booking."""
    _, tg = await _client(world)
    st, body = await api.post(
        tg,
        "/api/bookings",
        {"barber_id": world.ali_id, "service_id": None, "start_at": iso(L(MONDAY, "15:00"))},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert st == 400, (st, body)


async def test_crafted_off_step_start_is_refused(world: World, api: Api) -> None:
    """Ali's slot step is 30 min, so the app/bot only offer :00/:30 starts in an empty
    afternoon. A crafted 15:15 start is accepted and one 30-min booking then kills both
    the 15:00 and the 15:30 slot (capacity griefing, schedule fragmentation)."""
    _, tg = await _client(world)
    st, body = await api.post(
        tg,
        "/api/bookings",
        {"barber_id": world.ali_id, "service_id": world.svc30, "start_at": iso(L(MONDAY, "15:15"))},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert st == 409 and body["error"]["code"] == "slot_unavailable", (st, body)
