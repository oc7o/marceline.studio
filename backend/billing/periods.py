"""Billing periods of a subscription. Pure.

Periods are anchored at the start day and computed from it every time (never from the previous
period), so a subscription started on 31 January is billed for 31 Jan to 27 Feb, 28 Feb to
30 Mar, 31 Mar to 29 Apr, … instead of drifting to the 28th for good.
"""

import calendar
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Interval = Literal["month", "year"]
MONTHS = {"month": 1, "year": 12}


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # inclusive


def add_months(day: date, months: int) -> date:
    """`day` plus whole months, clamped to the last day of a shorter month."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def period(starts_on: date, interval: Interval, index: int) -> Period:
    step = MONTHS[interval]
    start = add_months(starts_on, index * step)
    end = add_months(starts_on, (index + 1) * step) - timedelta(days=1)
    return Period(start, end)


def due_periods(
    starts_on: date, interval: Interval, *, today: date, ends_on: date | None
) -> Iterator[Period]:
    """Every period that has started by `today` (billing is in advance) and before `ends_on`."""
    index = 0
    while True:
        current = period(starts_on, interval, index)
        if current.start > today or (ends_on is not None and current.start >= ends_on):
            return
        yield current
        index += 1
