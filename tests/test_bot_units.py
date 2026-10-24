"""Pure bot-layer pieces: i18n, callbacks, keyboards, parsing, error texts, router."""

from __future__ import annotations

import re
import string
from datetime import date, datetime, time, timedelta

import pytest

from app.bot import callbacks as cb
from app.bot import views
from app.bot.i18n import LOCALES, fmt_price, fmt_stamps, h, t
from app.bot.keyboards import barber as bkb
from app.bot.keyboards import client as ckb
from app.bot.keyboards.calendar import month_kb, parse_ym
from app.bot.keyboards.common import ALL_MENU_KEYS, menu_for
from app.db.enums import Awaiting, BookingStatus
from app.services import clock
from app.services.dto import DayAvailability, Roles
from app.services.errors import ALL_ERRORS, ClientLimitError, InputError, ServiceError

# ---------------------------------------------------------------- i18n


def _placeholders(s: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(s) if f}


def test_locale_key_parity() -> None:
    uz, ru = LOCALES["uz"], LOCALES["ru"]
    assert set(uz) == set(ru), set(uz) ^ set(ru)


def test_locale_placeholder_parity() -> None:
    uz, ru = LOCALES["uz"], LOCALES["ru"]
    bad = {k for k in uz if _placeholders(uz[k]) != _placeholders(ru.get(k, ""))}
    assert bad == set()


def test_templates_format_cleanly() -> None:
    for _lang, table in LOCALES.items():
        for _key, s in table.items():
            kw = {p: "x" for p in _placeholders(s)}
            s.format(**kw)  # no stray braces


def test_every_error_code_has_text() -> None:
    for cls in ALL_ERRORS:
        for lang in LOCALES:
            assert f"err.{cls.code}" in LOCALES[lang], (lang, cls.code)


def test_err_text_specific_keys() -> None:
    e = ClientLimitError("x", kind="max_active", limit=3)
    txt = views.err_text("uz", e)
    assert txt and "{" not in txt
    assert views.err_text("uz", ServiceError("x")) == t("uz", "err.error")
    assert views.err_text("ru", InputError("x", field="comment", limit=300))


def test_menu_labels_unique_per_locale() -> None:
    for lang in LOCALES:
        labels = [t(lang, k) for k in ALL_MENU_KEYS]
        assert len(labels) == len(set(labels)), lang


def test_h_escapes() -> None:
    assert h("<b>x</b> & y") == "&lt;b&gt;x&lt;/b&gt; &amp; y"
    assert h(None) == ""


def test_formatters() -> None:
    assert fmt_price(60000, "uz") == "60 000 so'm"
    assert fmt_stamps(2, 5) == "✂️✂️▫️▫️▫️"
    assert fmt_stamps(9, 5) == "✂️" * 5


def test_missing_key_and_placeholder() -> None:
    assert t("uz", "no.such.key") == "no.such.key"
    assert "{" not in t("uz", "common.minutes")


# ---------------------------------------------------------------- callbacks


MAXED = [
    cb.HubCb(a="rebook"),
    cb.SlotCb(b=2**31, s=2**31, d="20261026", t="1930"),
    cb.BookingCb(a="dback", id=2**31),
    cb.RejectCb(id=2**31, r="other"),
    cb.ProposeCb(id=2**31, d="20261026", t="1930"),
    cb.RebookCb(b=2**31, s=2**31),
    cb.RateCb(id=2**31, v=5),
    cb.BlockCb(a="cancelcf", d="20261026", t="1930"),
    cb.ManualCb(a="skipphone", s=2**31, d="20261026", t="1930"),
    cb.LinkCb(a="ok", b=2**31, u=2**31),
    cb.AdminCb(s="stat", a="override", id=2**31, v="20261026"),
    cb.PageCb(k="bookings", p=999, f="20261026-999999-CONFIRMED"),
    cb.CalCb(a="d", v="20261026"),
    cb.BarberCardCb(a="view", b=2**31),
]


@pytest.mark.parametrize("obj", MAXED, ids=lambda o: type(o).__name__)
def test_callback_size_and_roundtrip(obj) -> None:  # type: ignore[no-untyped-def]
    packed = obj.pack()
    assert len(packed.encode()) <= 64, packed
    assert type(obj).unpack(packed) == obj


def test_forged_callbacks_rejected() -> None:
    with pytest.raises((ValueError, TypeError)):
        cb.BookingCb.unpack("bk:acc:notanint")
    with pytest.raises((ValueError, TypeError)):
        cb.SlotCb.unpack("sl:1:1:20261026")


def test_all_keyboard_callbacks_fit() -> None:
    from aiogram.types import InlineKeyboardMarkup

    today = clock.local_today()
    days = [
        DayAvailability(day=today + timedelta(days=i), is_working=True, has_free_slots=True)
        for i in range(14)
    ]
    kbs: list[InlineKeyboardMarkup] = [
        ckb.days_kb(days, "ru", back=True, more=True),
        bkb.decision_kb(2**31, "ru"),
        bkb.reject_reasons_kb(2**31, "ru"),
        month_kb(
            today.year,
            today.month,
            available={today},
            first=today,
            last=today + timedelta(days=60),
            lang="ru",
        ),
    ]
    for k in kbs:
        for row in k.inline_keyboard:
            for b in row:
                if b.callback_data:
                    assert len(b.callback_data.encode()) <= 64


# ---------------------------------------------------------------- parsing helpers


def test_parse_day_and_hhmm() -> None:
    assert views.parse_day("20261026") == date(2026, 10, 26)
    for bad in ("2026102", "20261332", "abcdefgh", "", None):
        assert views.parse_day(bad) is None
    assert views.parse_hhmm("0930") == time(9, 30)
    for bad in ("2400", "1260", "930", "ab12", None):
        assert views.parse_hhmm(bad) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9", time(9)),
        ("9:00", time(9)),
        ("09.30", time(9, 30)),
        ("0930", time(9, 30)),
        ("930", time(9, 30)),
        ("24:00", None),
        ("25", None),
        ("9:61", None),
        ("abc", None),
    ],
)
def test_parse_time_text(raw: str, expected) -> None:  # type: ignore[no-untyped-def]
    assert views.parse_time_text(raw) == expected


def test_parse_time_range_and_date() -> None:
    assert views.parse_time_range("10:00-20:00") == (time(10), time(20))
    assert views.parse_time_range("10:00 – 13:30") == (time(10), time(13, 30))
    assert views.parse_time_range("20:00-10:00") is None
    today = date(2026, 10, 26)
    assert views.parse_date_text("26.10", today) == date(2026, 10, 26)
    assert views.parse_date_text("01.01", today) == date(2027, 1, 1)
    assert views.parse_date_text("26.10.26", today) == date(2026, 10, 26)
    assert views.parse_date_text("2026-10-27", today) == date(2026, 10, 27)
    assert views.parse_date_text("31.02", today) is None
    assert views.parse_date_text("hello", today) is None
    assert (
        parse_ym("202610") == (2026, 10) and parse_ym("202613") is None and parse_ym("2026") is None
    )


# ---------------------------------------------------------------- keyboards


def _cbs(k) -> list[str]:  # type: ignore[no-untyped-def]
    return [b.callback_data for row in k.inline_keyboard for b in row if b.callback_data]


def test_slots_kb_groups_and_rows() -> None:
    day = clock.local_today() + timedelta(days=1)
    slots = [clock.local_dt(day, time(h, m)) for h in (10, 11, 14, 15, 16, 18) for m in (0, 30)]
    k = ckb.slots_kb(slots, barber_id=1, service_id=2, day=day, lang="uz")
    labels = [[b.text for b in row] for row in k.inline_keyboard]
    assert labels[0] == [t("uz", "book.group_morning")]
    assert labels[1] == ["10:00", "10:30", "11:00", "11:30"]
    assert [t("uz", "book.group_day")] in labels and [t("uz", "book.group_evening")] in labels
    assert all(len(r) <= 4 for r in labels)
    assert f"sl:1:2:{day.strftime('%Y%m%d')}:1000" in _cbs(k)


def test_days_kb_hides_full_today_and_marks_full_days() -> None:
    today = clock.local_today()
    days = [
        DayAvailability(day=today, is_working=True, has_free_slots=False),
        DayAvailability(day=today + timedelta(days=1), is_working=True, has_free_slots=True),
        DayAvailability(day=today + timedelta(days=2), is_working=False, has_free_slots=False),
    ]
    k = ckb.days_kb(days, "uz", back=True, more=False)
    texts = [b.text for row in k.inline_keyboard for b in row]
    assert t("uz", "common.today") not in texts
    assert t("uz", "common.tomorrow") in texts
    assert any(
        x.startswith(t("uz", "common.day_after")) and t("uz", "book.day_full") in x for x in texts
    )


def test_result_kb_by_status() -> None:
    class B:  # minimal DTO stand-in
        def __init__(self, status, awaiting=None):  # type: ignore[no-untyped-def]
            self.id, self.status, self.awaiting = 7, status, awaiting

    assert "bk:wd:7" in _cbs(ckb.result_kb(B(BookingStatus.PENDING, Awaiting.BARBER), "uz"))
    assert "bk:ccx:7" in _cbs(ckb.result_kb(B(BookingStatus.CONFIRMED), "uz"))
    assert not {"bk:wd:7", "bk:ccx:7"} & set(_cbs(ckb.result_kb(B(BookingStatus.REJECTED), "uz")))


def test_menus_per_role() -> None:
    def labels(m) -> set[str]:  # type: ignore[no-untyped-def]
        return {b.text for row in m.keyboard for b in row}

    client = Roles(
        is_client=True, is_barber=False, is_admin=False, is_env_admin=False, barber_id=None
    )
    barber = Roles(is_client=True, is_barber=True, is_admin=False, is_env_admin=False, barber_id=1)
    admin = Roles(is_client=True, is_barber=False, is_admin=True, is_env_admin=True, barber_id=None)
    assert t("uz", "menu.role") not in labels(menu_for(client, "uz"))
    assert t("uz", "menu.b_today") in labels(menu_for(barber, "uz"))
    assert t("uz", "menu.a_barbers") in labels(menu_for(admin, "uz"))
    # a client can't force the admin view
    assert t("uz", "menu.a_barbers") not in labels(menu_for(client, "uz", "a"))


def test_proposal_kb() -> None:
    day = clock.local_today() + timedelta(days=1)
    k = bkb.proposal_kb(5, [clock.local_dt(day, time(11))], "uz")
    assert f"pp:5:{day.strftime('%Y%m%d')}:1100" in _cbs(k) and "bk:dback:5" in _cbs(k)


def test_month_kb_only_available_days_clickable() -> None:
    first = date(2026, 10, 26)
    k = month_kb(
        2026, 10, available={date(2026, 10, 27)}, first=first, last=date(2026, 11, 20), lang="uz"
    )
    day_cbs = [c for c in _cbs(k) if c.startswith("cal:d:")]
    assert day_cbs == ["cal:d:20261027"]
    assert "cal:m:202611" in _cbs(k) and "cal:m:202609" not in _cbs(k)


def test_status_and_reason_labels() -> None:
    class B:
        status, awaiting = BookingStatus.PENDING, Awaiting.CLIENT

    assert views.status_label("uz", B()) == t("uz", "status.PENDING_CLIENT")
    assert views.reason_label("uz", "<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"
    assert views.reason_label("uz", "sick") == t("uz", "reason.sick")


# ---------------------------------------------------------------- router / middlewares


def test_router_builds_twice() -> None:
    from app.bot.handlers import build_root_router

    r1 = build_root_router()
    r2 = build_root_router()
    assert r1 is not r2 and len(r2.sub_routers) >= 19


def test_debounce_window() -> None:
    from app.bot.middlewares import DebounceMiddleware

    d = DebounceMiddleware(window=1.0)
    assert not d.is_duplicate(1, "bk:acc:1", now=100.0)
    assert d.is_duplicate(1, "bk:acc:1", now=100.5)
    assert not d.is_duplicate(2, "bk:acc:1", now=100.6)
    assert not d.is_duplicate(1, "bk:acc:1", now=102.0)


def test_user_middleware_helpers() -> None:
    from app.bot.middlewares.user import _command, _start_payload

    assert _start_payload("/start inv_abc") == "inv_abc"
    assert _start_payload("/start") is None
    assert _start_payload("/startx foo") is None
    assert _command("/Menu@test_bot extra") == "/menu"
    assert _command("hello") is None


def test_booking_text_escapes_user_input() -> None:
    from app.services.dto import BookingDTO

    now = clock.now_utc()
    b = BookingDTO(
        id=1,
        status=BookingStatus.CONFIRMED,
        awaiting=None,
        source=__import__("app.db.enums", fromlist=["BookingSource"]).BookingSource.BOT,
        client_id=1,
        client_tg_id=1,
        client_lang="uz",
        client_name="<i>Ali</i>",
        client_phone="+998901234567",
        client_phone_verified=True,
        barber_id=1,
        barber_name="<b>B</b>",
        barber_tg_id=2,
        service_id=1,
        service_name="<s>S</s>",
        start_at=now,
        end_at=now + timedelta(minutes=30),
        duration_min=30,
        original_start_at=None,
        proposal_count=0,
        price_snapshot=60000,
        final_price_uzs=60000,
        bonus_cost=0,
        bonus_redeemed=False,
        comment="<b>soqol</b>",
        expire_at=None,
        late_cancel=False,
        client_ack_at=None,
        auto_completed=False,
        completed_at=None,
        visit_confirmed=None,
        cancelled_by=None,
        cancel_reason=None,
        created_at=now,
    )
    txt = views.booking_text(b, "uz", staff=True)
    assert "<b>soqol</b>" not in txt and "&lt;b&gt;soqol&lt;/b&gt;" in txt
    assert "<i>Ali</i>" not in txt and "<s>S</s>" not in txt
    assert re.search(r"<b>B</b>", txt) is None
    assert datetime


def test_throttle_window() -> None:
    from app.bot.middlewares import ThrottleMiddleware

    m = ThrottleMiddleware(per_minute=3)
    assert all(m.hit(1, now=10.0 + i) for i in range(3))
    assert not m.hit(1, now=14.0)
    assert m._first_overflow(1)
    assert not m.hit(1, now=15.0) and not m._first_overflow(1)
    assert m.hit(2, now=15.0)
    assert m.hit(1, now=80.0)  # window slid
