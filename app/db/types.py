"""Custom column types (ARCHITECTURE.md §3)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import DateTime, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Aware-UTC datetime.

    Stores naive UTC on SQLite and ``timestamptz`` on PostgreSQL. Always returns
    aware UTC and raises ``ValueError`` on naive input.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime expects datetime, got {type(value).__name__}")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime is not allowed; pass aware UTC")
        value = value.astimezone(timezone.utc)
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):  # defensive: raw sqlite text
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class StrEnumType(TypeDecorator[Enum]):
    """Stores ``enum.value`` in a ``String(length)`` and returns the enum member."""

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[Enum], length: int, **kw: Any) -> None:
        self.enum_cls = enum_cls
        self.length = length
        super().__init__(length, **kw)

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_cls):
            return str(value.value)
        # accept the raw value, validating it
        return str(self.enum_cls(value).value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Enum | None:
        if value is None:
            return None
        return self.enum_cls(value)

    def copy(self, **kw: Any) -> StrEnumType:
        return StrEnumType(self.enum_cls, self.length)
