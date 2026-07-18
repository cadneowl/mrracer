"""Business-hours time math.

This is the highest-risk logic in the project and is intentionally kept pure:
no I/O, no config parsing, no clocks pulled from the environment. Everything is
a function of its arguments so it can be exhaustively unit-tested, including
timezone and week-boundary edges.

Core idea: a work calendar defines which weekdays are workdays and the daily
work window (e.g. 09:00-18:00). "Business time" between two instants is the
number of seconds that fall inside those windows, measured in a given
timezone. Weekends and off-hours never count. DST transitions are handled
correctly because all arithmetic is done on timezone-aware datetimes, whose
subtraction yields real elapsed seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

__all__ = [
    "WorkCalendar",
    "WEEKDAY_NAMES",
    "parse_weekday",
    "parse_hhmm",
    "business_seconds_between",
    "business_hours_between",
    "add_business_hours",
]

# Monday == 0 to match datetime.weekday().
WEEKDAY_NAMES: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

_MAX_DAYS = 3660  # ~10 years; a safety bound for the forward walk.


def parse_weekday(name: str) -> int:
    """Map a three-letter weekday name (case-insensitive) to 0=Mon..6=Sun."""
    try:
        return WEEKDAY_NAMES[name.strip().lower()]
    except KeyError:
        raise ValueError(
            f"invalid weekday {name!r}; expected one of {sorted(WEEKDAY_NAMES)}"
        ) from None


def parse_hhmm(value: str) -> time:
    """Parse a 'HH:MM' 24-hour string into a time."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time {value!r}; expected 'HH:MM'")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"invalid time {value!r}; expected 'HH:MM'") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range {value!r}")
    return time(hour, minute)


@dataclass(frozen=True)
class WorkCalendar:
    """A weekly work calendar.

    workdays:   set of weekday integers (0=Mon..6=Sun) that are working days.
    work_start: daily start of the work window (local wall time).
    work_end:   daily end of the work window (local wall time); must be > start.
    """

    workdays: frozenset[int]
    work_start: time
    work_end: time

    def __post_init__(self) -> None:
        if not self.workdays:
            raise ValueError("work calendar must have at least one workday")
        if any(d < 0 or d > 6 for d in self.workdays):
            raise ValueError("workdays must be integers in 0..6")
        if self.work_end <= self.work_start:
            raise ValueError("work_hours.end must be after work_hours.start")

    def is_workday(self, weekday: int) -> bool:
        return weekday in self.workdays

    @property
    def seconds_per_workday(self) -> float:
        start = timedelta(hours=self.work_start.hour, minutes=self.work_start.minute)
        end = timedelta(hours=self.work_end.hour, minutes=self.work_end.minute)
        return (end - start).total_seconds()


def _require_aware(name: str, dt: datetime) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _window_bounds(local_date, cal: WorkCalendar, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Return the (open, close) aware datetimes of the work window on a date."""
    open_dt = datetime.combine(local_date, cal.work_start, tzinfo=tz)
    close_dt = datetime.combine(local_date, cal.work_end, tzinfo=tz)
    return open_dt, close_dt


def business_seconds_between(
    start: datetime,
    end: datetime,
    cal: WorkCalendar,
    tz: ZoneInfo,
) -> float:
    """Business seconds elapsed between two instants, measured in ``tz``.

    Both ``start`` and ``end`` must be timezone-aware (any zone; they are
    compared as instants). Returns 0.0 if ``end <= start``.
    """
    _require_aware("start", start)
    _require_aware("end", end)
    if end <= start:
        return 0.0

    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)

    total = 0.0
    day = start_local.date()
    last = end_local.date()
    while day <= last:
        if cal.is_workday(day.weekday()):
            open_dt, close_dt = _window_bounds(day, cal, tz)
            lo = max(start, open_dt)
            hi = min(end, close_dt)
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


def business_hours_between(
    start: datetime,
    end: datetime,
    cal: WorkCalendar,
    tz: ZoneInfo,
) -> float:
    """business_seconds_between expressed in hours."""
    return business_seconds_between(start, end, cal, tz) / 3600.0


def add_business_hours(
    start: datetime,
    hours: float,
    cal: WorkCalendar,
    tz: ZoneInfo,
) -> datetime:
    """Return the instant reached by consuming ``hours`` of business time from
    ``start`` (i.e. the SLA deadline for a budget of ``hours``).

    The result is returned in ``tz``. A non-positive budget clamps the start
    into the work window (the next moment the clock would begin running).
    """
    _require_aware("start", start)
    if hours < 0:
        raise ValueError("hours must be non-negative")

    remaining = hours * 3600.0
    cursor = start.astimezone(tz)
    day = cursor.date()

    for _ in range(_MAX_DAYS):
        if cal.is_workday(day.weekday()):
            open_dt, close_dt = _window_bounds(day, cal, tz)
            seg_start = max(cursor, open_dt)
            if seg_start < close_dt:
                avail = (close_dt - seg_start).total_seconds()
                if avail >= remaining:
                    return seg_start + timedelta(seconds=remaining)
                remaining -= avail
        # advance to the start of the next calendar day in tz
        day += timedelta(days=1)
        cursor = datetime.combine(day, cal.work_start, tzinfo=tz)

    raise RuntimeError(
        f"could not consume {hours} business hours within {_MAX_DAYS} days; "
        "check the work calendar"
    )
