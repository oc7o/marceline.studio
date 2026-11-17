"""How fast a provider reacts to our notices, and a takedown-speed band. Pure; thresholds named.

Two durations per notice (`catalog.HosterContact`):

- **time to first response** — `sent_at` → `responded_at`;
- **time to removal** — `sent_at` → `removed_observed_at`: the first availability check that found
  every page the notice concerns gone (stamped by `marceline.hosting.observe_notice_removals`).
  Where nothing was observed but the notice was recorded as `removed` with a reply, the reply is
  the best date we have and stands in for the observation.

**Observed removals are only as precise as the recheck interval** (`crawl_recheck`, 24 h): a page
taken down an hour after the notice is seen gone at the next check. The durations here are
day-grained in practice, and `VERY_FAST_MAX` sits exactly at that grain on purpose.

A notice that never got an answer or a removal is a sample too — "never" — but only once it is
older than `OPEN_NOTICE_WINDOW` (or its outcome says so): a notice sent yesterday is not slow yet.
Younger open notices are left out of both samples. "Never" sorts after every duration.

Percentiles take the **upper nearest rank** (never interpolated): with a handful of notices and
"never" in the sample, an interpolated median would be a number nobody observed, and the upper
rank never makes a provider look faster than it was.

Exact numbers and the `takedown_speed` band are internal (Django admin). A customer sees
`catalog.hosting.history.customer_history` only: a rounded share and a floored time bucket
(operator decision 2026-09-17, ADR 0027 amendment).
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from catalog.hosting.cooperation import MIN_DECIDED_NOTICES

#: Ordered from worst to best, `unknown` first. The UI may rely on this order.
TakedownSpeed = Literal["unknown", "very_slow", "slow", "moderate", "fast", "very_fast"]
TAKEDOWN_SPEEDS: tuple[TakedownSpeed, ...] = (
    "unknown",
    "very_slow",
    "slow",
    "moderate",
    "fast",
    "very_fast",
)

#: Fewer timed removals (including "never") than this and the band is `unknown`.
MIN_SPEED_SAMPLES = MIN_DECIDED_NOTICES
#: Median time to removal at or below which a band applies; beyond `SLOW_MAX` (or never):
#: `very_slow`.
VERY_FAST_MAX = timedelta(hours=24)
FAST_MAX = timedelta(hours=72)
MODERATE_MAX = timedelta(days=7)
SLOW_MAX = timedelta(days=30)
#: An open notice younger than this is not counted as "never responded" / "never removed".
OPEN_NOTICE_WINDOW = timedelta(days=30)

#: "Never": sorts after every real duration.
NEVER = timedelta.max

#: Outcomes that settle "no answer" / "not removed" before the window has passed.
NO_RESPONSE_OUTCOMES = frozenset({"no_response"})
NOT_REMOVED_OUTCOMES = frozenset({"refused", "no_response"})


@dataclass(frozen=True)
class NoticeTimes:
    """What the aggregation needs of one notice."""

    sent_at: datetime
    responded_at: datetime | None = None
    removed_observed_at: datetime | None = None
    outcome: str = "awaiting"


@dataclass(frozen=True)
class SpeedStats:
    """Internal reaction statistics of one provider. `None`: no sample; `NEVER`: not reached."""

    response_samples: int = 0
    responded: int = 0
    median_to_response: timedelta | None = None
    p90_to_response: timedelta | None = None
    removal_samples: int = 0
    removals_timed: int = 0
    median_to_removal: timedelta | None = None
    p90_to_removal: timedelta | None = None

    @property
    def share_responded(self) -> float | None:
        return self.responded / self.response_samples if self.response_samples else None


def percentile(values: Sequence[timedelta], q: float) -> timedelta | None:
    """Upper nearest rank of `values` at `q` (0 < q < 1); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.floor(q * len(ordered)))]


def _duration(start: datetime, end: datetime) -> timedelta:
    return max(end - start, timedelta(0))


def time_to_response(notice: NoticeTimes, now: datetime) -> timedelta | None:
    """The sample this notice contributes, `NEVER`, or None while it is too young to say."""
    if notice.responded_at is not None:
        return _duration(notice.sent_at, notice.responded_at)
    if notice.outcome in NO_RESPONSE_OUTCOMES or now - notice.sent_at >= OPEN_NOTICE_WINDOW:
        return NEVER
    return None


def time_to_removal(notice: NoticeTimes, now: datetime) -> timedelta | None:
    """Observed removal first, a recorded `removed` reply second; `NEVER`; or None (too young)."""
    if notice.removed_observed_at is not None:
        return _duration(notice.sent_at, notice.removed_observed_at)
    if notice.outcome == "removed":
        # Removed by the provider's own account; without a reply date there is nothing to time.
        return _duration(notice.sent_at, notice.responded_at) if notice.responded_at else None
    if notice.outcome in NOT_REMOVED_OUTCOMES or now - notice.sent_at >= OPEN_NOTICE_WINDOW:
        return NEVER
    return None


def speed_stats(notices: Iterable[NoticeTimes], now: datetime) -> SpeedStats:
    responses: list[timedelta] = []
    removals: list[timedelta] = []
    for notice in notices:
        if (response := time_to_response(notice, now)) is not None:
            responses.append(response)
        if (removal := time_to_removal(notice, now)) is not None:
            removals.append(removal)
    return SpeedStats(
        response_samples=len(responses),
        responded=sum(1 for value in responses if value != NEVER),
        median_to_response=percentile(responses, 0.5),
        p90_to_response=percentile(responses, 0.9),
        removal_samples=len(removals),
        removals_timed=sum(1 for value in removals if value != NEVER),
        median_to_removal=percentile(removals, 0.5),
        p90_to_removal=percentile(removals, 0.9),
    )


def takedown_speed(stats: SpeedStats) -> TakedownSpeed:
    """The band of the median time to removal; `unknown` below `MIN_SPEED_SAMPLES`."""
    median = stats.median_to_removal
    if stats.removal_samples < MIN_SPEED_SAMPLES or median is None:
        return "unknown"
    if median <= VERY_FAST_MAX:
        return "very_fast"
    if median <= FAST_MAX:
        return "fast"
    if median <= MODERATE_MAX:
        return "moderate"
    if median <= SLOW_MAX:
        return "slow"
    return "very_slow"


def format_duration(value: timedelta | None) -> str:
    """For the admin: `-` (no sample), `never`, hours below three days, else days."""
    if value is None:
        return "-"
    if value == NEVER:
        return "never"
    hours = value.total_seconds() / 3600
    return f"{hours:.0f} h" if hours < 72 else f"{hours / 24:.1f} d"
