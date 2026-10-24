"""CallbackData factories (ARCHITECTURE.md §7).

Every packed string must fit into Telegram's 64-byte limit. Handlers never trust
these values: every id is re-validated by the services (permission + state).
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

NOOP = "noop"


class HubCb(CallbackData, prefix="h"):
    a: str  # book, my, bonus, info, prices, rebook, ref, lang, home


class LangCb(CallbackData, prefix="lg"):
    l: str  # noqa: E741  (contract field name) uz | ru


class ConsentCb(CallbackData, prefix="cs"):
    a: str  # ok


class SvcCb(CallbackData, prefix="sv"):
    s: int  # 0 = none


class BarberCb(CallbackData, prefix="bb"):
    b: int  # 0 = any barber


class BarberCardCb(CallbackData, prefix="bc"):
    a: str  # view, book, back
    b: int


class DayCb(CallbackData, prefix="dy"):
    d: str  # YYYYMMDD


class CalCb(CallbackData, prefix="cal"):
    a: str  # m (month nav) | d (day pick)
    v: str  # YYYYMM | YYYYMMDD


class SlotCb(CallbackData, prefix="sl"):
    b: int  # barber id (0 = any)
    s: int  # service id (0 = none)
    d: str  # YYYYMMDD
    t: str  # HHMM


class WizCb(CallbackData, prefix="wz"):
    a: str  # back, cancel, skip_comment, confirm, bonus_on, bonus_off, edit_name


class BookingCb(CallbackData, prefix="bk"):
    a: str  # acc rej prop card done ns scx ccx ccy wd pya pno ack vy vn dback
    id: int


class RejectCb(CallbackData, prefix="rj"):
    id: int
    r: str  # busy, sick, other, none


class ProposeCb(CallbackData, prefix="pp"):
    id: int
    d: str
    t: str


class RebookCb(CallbackData, prefix="rb"):
    b: int
    s: int  # 0 = none


class RateCb(CallbackData, prefix="rt"):
    id: int
    v: int


class QueueDayCb(CallbackData, prefix="qd"):
    d: str  # YYYYMMDD; "" = day picker


class BusyCb(CallbackData, prefix="bz"):
    m: int  # 30 | 60


class BlockCb(CallbackData, prefix="bl"):
    # contract: day, whole, start, end, cancelcf, keep, abort
    # bot-local extras: noreason (skip reason), list, del (t = time-off id), menu
    a: str
    d: str = ""
    t: str = ""


class ManualCb(CallbackData, prefix="mn"):
    a: str  # svc, day, slot, skipphone, confirm, abort
    s: int = 0
    d: str = ""
    t: str = ""


class LinkCb(CallbackData, prefix="lk"):
    a: str  # ok | no
    b: int
    u: int


class AdminCb(CallbackData, prefix="ad"):
    s: str  # br sv wh to bk cl st stat adm home
    a: str
    id: int = 0
    v: str = ""


class RoleCb(CallbackData, prefix="rl"):
    r: str  # c | b | a


class PageCb(CallbackData, prefix="pg"):
    k: str
    p: int
    f: str = ""


class DelMeCb(CallbackData, prefix="dm"):
    """Bot-local addition: /delete_me confirmation (yes | no)."""

    a: str
