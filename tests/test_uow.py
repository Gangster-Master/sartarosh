from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.db.models import Service
from app.db.uow import read_tx, write_tx
from app.services.errors import ServiceError
from tests.factories import count_rows


async def test_write_tx_rolls_back_after_flush(sm) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ServiceError):
        async with write_tx(sm) as s:
            s.add(Service(name="X", duration_min=30, price_uzs=1))
            await s.flush()
            raise ServiceError("boom")
    assert await count_rows(sm, Service) == 0


async def test_write_tx_commits(sm) -> None:  # type: ignore[no-untyped-def]
    async with write_tx(sm) as s:
        s.add(Service(name="X", duration_min=30, price_uzs=1))
    assert await count_rows(sm, Service) == 1


async def test_read_tx_leaves_no_writes(sm) -> None:  # type: ignore[no-untyped-def]
    async with read_tx(sm) as s:
        s.add(Service(name="X", duration_min=30, price_uzs=1))
        await s.flush()
    assert await count_rows(sm, Service) == 0


async def test_constraints_fire(sm) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with write_tx(sm) as s:
            s.add(Service(name="X", duration_min=20, price_uzs=1))


# ---------------------------------------------------------------- AST no-I/O-in-tx [A2]

APP_BOT = Path(__file__).resolve().parents[1] / "app" / "bot"
IO_ROOTS = {"bot", "message", "call", "notifier", "screen", "views", "state", "target"}
IO_ALLOWED = {"views.err_text", "views.status_label", "views.booking_text", "views.booking_lines"}


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Call):
        return _root_name(node.func)
    return node.id if isinstance(node, ast.Name) else None


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _tx_blocks(tree: ast.AST):  # type: ignore[no-untyped-def]
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncWith):
            for item in node.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call) and _dotted(ce.func).split(".")[-1] in (
                    "write_tx",
                    "read_tx",
                ):
                    yield node


def test_no_telegram_io_inside_tx_blocks() -> None:
    offenders = []
    for path in APP_BOT.rglob("*.py"):
        if path.name == "fsm_storage.py":
            continue  # the storage itself owns its short transactions
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for block in _tx_blocks(tree):
            for stmt in block.body:
                for node in ast.walk(stmt):
                    if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                        func = node.value.func
                        root = _root_name(func)
                        if root in IO_ROOTS and _dotted(func) not in IO_ALLOWED:
                            offenders.append(f"{path.name}:{node.lineno} {_dotted(func)}")
                        # helpers that receive a Telegram object can do I/O too
                        args = list(node.value.args) + [k.value for k in node.value.keywords]
                        if any(
                            isinstance(a, ast.Name) and a.id in IO_ROOTS - {"views", "screen"}
                            for a in args
                        ):
                            offenders.append(f"{path.name}:{node.lineno} {_dotted(func)}(<tg obj>)")
    assert offenders == []
