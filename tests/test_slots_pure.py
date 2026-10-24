from __future__ import annotations

from datetime import time, timedelta

from app.services.dto import Interval, WorkingHoursDTO
from app.services.slots import compute_free_slots
from tests.factories import NOW, D, L

HOURS = WorkingHoursDTO(
    barber_id=1,
    weekday=0,
    start_time=time(10),
    end_time=time(20),
    break_start=time(13),
    break_end=time(14),
)


def free(
    day=None, *, blocks=(), duration=30, buffer=0, step=30, now=NOW, lead=30, hours=HOURS, last=None
):  # type: ignore[no-untyped-def]
    day = day or D(1)
    return compute_free_slots(
        day=day,
        hours=hours,
        blocks=list(blocks),
        duration_min=duration,
        buffer_min=buffer,
        step_min=step,
        now=now,
        min_lead_min=lead,
        last_bookable_day=last or D(6),
    )


def hm(slots, day=None):  # type: ignore[no-untyped-def]
    from app.services import clock

    return [clock.to_local(s).strftime("%H:%M") for s in slots]


def test_full_day_30min() -> None:
    out = hm(free())
    assert out[0] == "10:00" and out[-1] == "19:30"
    assert "13:00" not in out and "13:30" not in out  # break
    assert "12:30" in out and "14:00" in out
    assert len(out) == 18


def test_60min_crosses_break_and_close() -> None:
    out = hm(free(duration=60))
    assert "12:30" not in out  # would run into the break
    assert "19:30" not in out  # would run past close
    assert "12:00" in out and "19:00" in out


def test_60min_needs_two_steps() -> None:
    blk = Interval(L(D(1), "11:00"), L(D(1), "11:30"))
    out = hm(free(duration=60, blocks=[blk]))
    assert "10:30" not in out and "11:00" not in out
    assert "10:00" in out and "11:30" in out


def test_time_off_excluded() -> None:
    blk = Interval(L(D(1), "15:00"), L(D(1), "17:00"))
    out = hm(free(blocks=[blk]))
    assert not {"15:00", "15:30", "16:00", "16:30"} & set(out)
    assert "14:30" in out and "17:00" in out


def test_lead_time_ceil_to_grid() -> None:
    now = L(D(0), "09:50")  # +30 = 10:20 -> ceil 10:30
    out = hm(free(D(0), now=now))
    assert out[0] == "10:30"
    now = L(D(0), "19:31")
    assert free(D(0), now=now) == []


def test_past_day_and_horizon() -> None:
    assert free(D(-1)) == []
    assert free(D(7), last=D(6)) == []
    assert free(D(6), last=D(6)) != []


def test_day_off() -> None:
    assert free(hours=None) == []


def test_half_open_edge() -> None:
    blk = Interval(L(D(1), "10:00"), L(D(1), "10:30"))
    out = hm(free(blocks=[blk]))
    assert "10:00" not in out and "10:30" in out


def test_buffer_blocks_next_15() -> None:
    # existing booking 10:00-10:30 with buffer 15 -> block to 10:45; candidates incl. 10:45
    blk = Interval(L(D(1), "10:00"), L(D(1), "10:45"))
    out = hm(free(blocks=[blk], buffer=15))
    assert "10:30" not in out
    assert "10:45" in out  # end-of-block candidate
    # own buffer: a 30-min slot at 12:30 + 15 buffer runs into the break
    assert "12:30" not in out
    # buffer may run past closing time
    assert "19:30" in out


def test_end_of_block_candidate() -> None:
    blk = Interval(L(D(1), "10:15"), L(D(1), "10:45"))
    out = hm(free(blocks=[blk]))
    assert "10:00" not in out and "10:30" not in out
    assert "10:45" in out


def test_all_on_grid_sorted() -> None:
    from app.services import clock

    out = free(blocks=[Interval(L(D(1), "10:15"), L(D(1), "10:45"))])
    assert out == sorted(out)
    assert all(clock.is_on_grid(t) for t in out)
    assert all(t.utcoffset() == timedelta(0) for t in out)
