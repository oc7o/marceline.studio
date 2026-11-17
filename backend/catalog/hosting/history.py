"""A provider's response history as a customer may see it: rounded, past tense. Pure; rules named.

Operator decision 2026-09-17 (ADR 0027 amendment):
the takedown-chance and takedown-speed bands read as predictions nobody can substantiate, so a
customer no longer sees them. What they see instead is what happened to notices recorded with the
provider so far, deliberately coarse:

- **the removal share, rounded to the nearest 5 %** — "about 75 % removed", never "3 of 4";
- **the median time to removal, floored into a bucket** — "same day", "1+ day", "3+ days",
  "7+ days", "14+ days", "30+ days", never hours or days as a number;
- **nothing at all below a minimum number of observed notices** (`HOSTING_HISTORY_MIN_NOTICES`,
  passed in by the caller) — "no response history yet". The time bucket additionally needs
  `MIN_TIMED_REMOVALS` removals with a date, so one quick removal never reads as "usually".

A notice is **observed** once its removal question is settled, with the rules of
`catalog.hosting.speed.time_to_removal`: removed (seen gone by an availability check, or recorded
as removed by the provider), or not removed (refused, no response, or older than
`OPEN_NOTICE_WINDOW` with nothing seen). A young open notice is not observed yet — it is neither a
success nor a failure. A removal recorded without any date counts towards the share but cannot be
timed.

The exact counts and durations stay internal (`speed.SpeedStats`, `cooperation.Cooperation`, the
Django admin). The median here is over **timed removals only** — the share already says how many
were not removed — and takes the upper nearest rank like every percentile in `speed`.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from catalog.hosting.speed import NEVER, NoticeTimes, percentile, time_to_removal

#: Floor buckets of the median time to removal, fastest first. Stable codes; the UI owns the words.
RemovalTime = Literal[
    "same_day", "1_plus_days", "3_plus_days", "7_plus_days", "14_plus_days", "30_plus_days"
]
#: (lower bound, bucket), from the slowest bucket down: the first bound the median reaches wins.
REMOVAL_TIME_FLOORS: tuple[tuple[timedelta, RemovalTime], ...] = (
    (timedelta(days=30), "30_plus_days"),
    (timedelta(days=14), "14_plus_days"),
    (timedelta(days=7), "7_plus_days"),
    (timedelta(days=3), "3_plus_days"),
    (timedelta(days=1), "1_plus_days"),
    (timedelta(0), "same_day"),
)
REMOVAL_TIMES: tuple[RemovalTime, ...] = tuple(
    bucket for _, bucket in reversed(REMOVAL_TIME_FLOORS)
)

#: Default for the `HOSTING_HISTORY_MIN_NOTICES` setting: fewer observed notices, no history.
DEFAULT_MIN_NOTICES = 5
#: Fewer removals with a date than this and the time bucket is left out ("usually" needs a few).
MIN_TIMED_REMOVALS = 3
#: The removal share is rounded to a multiple of this many percent.
PERCENT_STEP = 5


@dataclass(frozen=True)
class ResponseHistory:
    """Internal: observed notices, how many of them ended removed, and the timed removals."""

    observed: int = 0
    removed: int = 0
    removal_times: tuple[timedelta, ...] = field(default=())


@dataclass(frozen=True)
class CustomerHistory:
    """What a customer sees: a rounded percentage and, where there are enough dates, a bucket."""

    removed_percent: int
    usual_removal_time: RemovalTime | None


def response_history(notices: Iterable[NoticeTimes], now: datetime) -> ResponseHistory:
    observed = removed = 0
    times: list[timedelta] = []
    for notice in notices:
        duration = time_to_removal(notice, now)
        if duration is None:
            if notice.outcome == "removed":  # removed by the provider's account, no date to time
                observed += 1
                removed += 1
            continue
        observed += 1
        if duration != NEVER:
            removed += 1
            times.append(duration)
    return ResponseHistory(observed=observed, removed=removed, removal_times=tuple(times))


def rounded_percent(part: int, whole: int) -> int:
    """`part / whole` as a percentage rounded to the nearest `PERCENT_STEP`, halves up."""
    if whole <= 0:
        raise ValueError("whole must be positive")
    steps = 100 // PERCENT_STEP
    return (2 * part * steps + whole) // (2 * whole) * PERCENT_STEP


def removal_time_bucket(median: timedelta) -> RemovalTime:
    """Floor a duration into its bucket: 2 days 23 hours is "1+ day", never "3+ days"."""
    for floor, bucket in REMOVAL_TIME_FLOORS:
        if median >= floor:
            return bucket
    return "same_day"


def customer_history(history: ResponseHistory, min_notices: int) -> CustomerHistory | None:
    """None below `min_notices` observed notices: "no response history yet", and nothing else."""
    if history.observed < max(min_notices, 1):
        return None
    median = (
        percentile(history.removal_times, 0.5)
        if len(history.removal_times) >= MIN_TIMED_REMOVALS
        else None
    )
    return CustomerHistory(
        removed_percent=rounded_percent(history.removed, history.observed),
        usual_removal_time=removal_time_bucket(median) if median is not None else None,
    )
