"""Exhaustive tests for the business-hours module — the project's riskiest logic.

Covers window clipping, weekend/holiday skipping, week and multi-week
boundaries, timezone conversion, DST spring-forward / fall-back, the
add_business_hours inverse, and input validation.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from radar.business_time import (
    WorkCalendar,
    add_business_hours,
    business_hours_between,
    parse_hhmm,
    parse_weekday,
)

NY = ZoneInfo("America/New_York")
JLM = ZoneInfo("Asia/Jerusalem")
UTC = UTC

# Standard calendar: Mon-Fri, 09:00-18:00 (a 9-hour workday).
STD = WorkCalendar(
    workdays=frozenset({0, 1, 2, 3, 4}),
    work_start=time(9, 0),
    work_end=time(18, 0),
)


def ny(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY)


# --- parsing ---------------------------------------------------------------


def test_parse_weekday_ok():
    assert parse_weekday("mon") == 0
    assert parse_weekday("SUN") == 6
    assert parse_weekday(" Thu ") == 3


def test_parse_weekday_bad():
    with pytest.raises(ValueError):
        parse_weekday("funday")


def test_parse_hhmm():
    assert parse_hhmm("09:00") == time(9, 0)
    assert parse_hhmm("23:59") == time(23, 59)
    for bad in ["9", "24:00", "09:60", "aa:bb"]:
        with pytest.raises(ValueError):
            parse_hhmm(bad)


def test_calendar_validation():
    with pytest.raises(ValueError):
        WorkCalendar(frozenset(), time(9), time(18))
    with pytest.raises(ValueError):
        WorkCalendar(frozenset({0}), time(18), time(9))  # end <= start
    with pytest.raises(ValueError):
        WorkCalendar(frozenset({7}), time(9), time(18))  # weekday out of range


def test_seconds_per_workday():
    assert STD.seconds_per_workday == 9 * 3600


def test_add_negative_hours_rejected():
    with pytest.raises(ValueError):
        add_business_hours(ny(2026, 3, 2, 10), -1, STD, NY)


def test_add_requires_aware():
    with pytest.raises(ValueError):
        add_business_hours(datetime(2026, 3, 2, 10), 1, STD, NY)


# --- same-day windows ------------------------------------------------------


def test_simple_within_window():
    # Monday 2026-03-02, 10:00 -> 12:00 = 2h
    assert business_hours_between(ny(2026, 3, 2, 10), ny(2026, 3, 2, 12), STD, NY) == 2.0


def test_clip_before_open():
    # 08:00 -> 10:00 counts from 09:00 = 1h
    assert business_hours_between(ny(2026, 3, 2, 8), ny(2026, 3, 2, 10), STD, NY) == 1.0


def test_clip_after_close():
    # 17:00 -> 20:00 counts to 18:00 = 1h
    assert business_hours_between(ny(2026, 3, 2, 17), ny(2026, 3, 2, 20), STD, NY) == 1.0


def test_full_day_saturates():
    # 06:00 -> 22:00 = full 9h window
    assert business_hours_between(ny(2026, 3, 2, 6), ny(2026, 3, 2, 22), STD, NY) == 9.0


def test_entirely_off_hours():
    assert business_hours_between(ny(2026, 3, 2, 19), ny(2026, 3, 2, 23), STD, NY) == 0.0


# --- weekends & spanning ---------------------------------------------------


def test_weekend_skipped():
    # Fri 2026-03-06 16:00 -> Mon 2026-03-09 10:00
    # Fri 16-18 = 2h, weekend skipped, Mon 9-10 = 1h => 3h
    assert business_hours_between(ny(2026, 3, 6, 16), ny(2026, 3, 9, 10), STD, NY) == 3.0


def test_saturday_only_is_zero():
    assert business_hours_between(ny(2026, 3, 7, 9), ny(2026, 3, 7, 18), STD, NY) == 0.0


def test_one_full_work_week():
    # Mon 09:00 -> next Mon 09:00 = 5 workdays * 9h = 45h
    assert business_hours_between(ny(2026, 3, 2, 9), ny(2026, 3, 9, 9), STD, NY) == 45.0


def test_two_full_work_weeks():
    # Mon 09:00 -> Mon 09:00 two weeks later = 10 workdays * 9h = 90h
    assert business_hours_between(ny(2026, 3, 2, 9), ny(2026, 3, 16, 9), STD, NY) == 90.0


def test_mid_week_multi_day():
    # Mon 15:00 -> Wed 11:00: Mon 15-18=3h, Tue 9-18=9h, Wed 9-11=2h => 14h
    assert business_hours_between(ny(2026, 3, 2, 15), ny(2026, 3, 4, 11), STD, NY) == 14.0


# --- degenerate intervals --------------------------------------------------


def test_zero_and_reversed():
    t = ny(2026, 3, 2, 10)
    assert business_hours_between(t, t, STD, NY) == 0.0
    assert business_hours_between(ny(2026, 3, 2, 12), ny(2026, 3, 2, 10), STD, NY) == 0.0


def test_requires_aware():
    naive = datetime(2026, 3, 2, 10)
    with pytest.raises(ValueError):
        business_hours_between(naive, ny(2026, 3, 2, 12), STD, NY)
    with pytest.raises(ValueError):
        business_hours_between(ny(2026, 3, 2, 10), naive, STD, NY)


# --- timezone correctness --------------------------------------------------


def test_timezone_conversion_from_utc():
    # March 2 2026 is EST (UTC-5, before DST). 14:00Z = 09:00 ET, 22:00Z = 17:00 ET => 8h
    start = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
    end = datetime(2026, 3, 2, 22, 0, tzinfo=UTC)
    assert business_hours_between(start, end, STD, NY) == 8.0


def test_reviewer_timezone_independent_of_input_zone():
    # Same instants, measured against a Jerusalem calendar. Provide UTC instants
    # that map to 09:00-13:00 local Jerusalem on a Tuesday.
    # 2026-07-07 is a Tuesday; Jerusalem is UTC+3 in July (IDT).
    start = datetime(2026, 7, 7, 6, 0, tzinfo=UTC)  # 09:00 IDT
    end = datetime(2026, 7, 7, 10, 0, tzinfo=UTC)  # 13:00 IDT
    assert business_hours_between(start, end, STD, JLM) == 4.0


# --- DST edges -------------------------------------------------------------


def test_dst_normal_workday_after_spring_forward():
    # Wed 2026-03-11 (after 03-08 spring forward). Nominal 9-18 window unaffected.
    assert business_hours_between(ny(2026, 3, 11, 6), ny(2026, 3, 11, 22), STD, NY) == 9.0


def test_dst_spring_forward_inside_window_loses_hour():
    # A contrived calendar whose window straddles the 02:00->03:00 gap.
    cal = WorkCalendar(frozenset({6}), time(1, 0), time(5, 0))  # Sundays 01:00-05:00
    # 2026-03-08 is the spring-forward Sunday. 01:00-05:00 spans a missing hour
    # => only 3 real hours elapse.
    assert business_hours_between(ny(2026, 3, 8, 0), ny(2026, 3, 8, 23), cal, NY) == 3.0


def test_dst_fall_back_inside_window_gains_hour():
    cal = WorkCalendar(frozenset({6}), time(1, 0), time(5, 0))  # Sundays 01:00-05:00
    # 2026-11-01 is the fall-back Sunday; 01:00-05:00 spans a repeated hour
    # => 5 real hours elapse.
    assert business_hours_between(ny(2026, 11, 1, 0), ny(2026, 11, 1, 23), cal, NY) == 5.0


# --- add_business_hours (deadline) -----------------------------------------


def test_add_business_hours_within_day():
    deadline = add_business_hours(ny(2026, 3, 2, 10), 3, STD, NY)
    assert deadline == ny(2026, 3, 2, 13)


def test_add_business_hours_rolls_over_days_and_weekend():
    # Fri 16:00 + 4h: Fri 16-18 (2h), Mon 9-11 (2h) => Mon 11:00
    deadline = add_business_hours(ny(2026, 3, 6, 16), 4, STD, NY)
    assert deadline == ny(2026, 3, 9, 11)


def test_add_business_hours_from_off_hours_starts_next_open():
    # Sat 12:00 + 2h => Mon 11:00
    deadline = add_business_hours(ny(2026, 3, 7, 12), 2, STD, NY)
    assert deadline == ny(2026, 3, 9, 11)


@pytest.mark.parametrize("hours", [0.5, 1, 4, 9, 12, 20, 45])
def test_add_is_inverse_of_between(hours):
    start = ny(2026, 3, 2, 9, 30)
    deadline = add_business_hours(start, hours, STD, NY)
    assert business_hours_between(start, deadline, STD, NY) == pytest.approx(hours, abs=1e-6)


def test_add_zero_hours():
    # Non-positive budget clamps into the work window (here already inside).
    assert add_business_hours(ny(2026, 3, 2, 10), 0, STD, NY) == ny(2026, 3, 2, 10)
