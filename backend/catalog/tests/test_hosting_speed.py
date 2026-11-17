"""The pure reaction-speed statistics and the takedown-speed band (ADR 0027 amendment)."""

from datetime import UTC, datetime, timedelta

import pytest

from catalog.hosting.cooperation import MIN_DECIDED_NOTICES
from catalog.hosting.speed import (
    FAST_MAX,
    MIN_SPEED_SAMPLES,
    MODERATE_MAX,
    NEVER,
    OPEN_NOTICE_WINDOW,
    SLOW_MAX,
    TAKEDOWN_SPEEDS,
    VERY_FAST_MAX,
    NoticeTimes,
    SpeedStats,
    format_duration,
    percentile,
    speed_stats,
    takedown_speed,
    time_to_removal,
    time_to_response,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
OLD = NOW - OPEN_NOTICE_WINDOW - timedelta(days=1)
H = timedelta(hours=1)


def answered(hours: float, *, removed_after: float | None = None) -> NoticeTimes:
    sent = OLD
    return NoticeTimes(
        sent_at=sent,
        responded_at=sent + hours * H,
        removed_observed_at=sent + removed_after * H if removed_after is not None else None,
        outcome="removed" if removed_after is not None else "refused",
    )


def test_the_band_codes_are_ordered_worst_to_best_and_reuse_the_chance_minimum() -> None:
    assert TAKEDOWN_SPEEDS == ("unknown", "very_slow", "slow", "moderate", "fast", "very_fast")
    assert MIN_SPEED_SAMPLES == MIN_DECIDED_NOTICES == 3


@pytest.mark.parametrize(
    ("hours", "q", "expected"),
    [
        ([5], 0.5, 5),
        ([5], 0.9, 5),
        ([1, 9], 0.5, 9),  # even count: the upper middle, never an interpolated 5
        ([3, 1, 2], 0.5, 2),
        ([3, 1, 2], 0.9, 3),
        ([1, 2, 3, 4], 0.5, 3),
        (list(range(1, 11)), 0.9, 10),
        (list(range(1, 21)), 0.9, 19),
    ],
)
def test_percentile_is_the_upper_nearest_rank(hours: list[int], q: float, expected: int) -> None:
    assert percentile([value * H for value in hours], q) == expected * H


def test_percentile_of_nothing_and_with_never() -> None:
    assert percentile([], 0.5) is None
    assert percentile([2 * H, NEVER, NEVER], 0.5) == NEVER
    assert percentile([2 * H, 4 * H, NEVER], 0.5) == 4 * H


def test_no_notices_no_numbers_and_unknown() -> None:
    stats = speed_stats([], NOW)
    assert stats == SpeedStats()
    assert stats.share_responded is None
    assert takedown_speed(stats) == "unknown"


def test_small_sample_medians_and_p90() -> None:
    stats = speed_stats(
        [
            answered(2, removed_after=20),
            answered(10, removed_after=30),
            answered(4, removed_after=100),
        ],
        NOW,
    )
    assert (stats.response_samples, stats.responded, stats.share_responded) == (3, 3, 1.0)
    assert (stats.median_to_response, stats.p90_to_response) == (4 * H, 10 * H)
    assert (stats.removal_samples, stats.removals_timed) == (3, 3)
    assert (stats.median_to_removal, stats.p90_to_removal) == (30 * H, 100 * H)
    assert takedown_speed(stats) == "fast"


def test_a_young_open_notice_is_not_counted_as_never() -> None:
    fresh = NoticeTimes(sent_at=NOW - OPEN_NOTICE_WINDOW + H)
    assert time_to_response(fresh, NOW) is None
    assert time_to_removal(fresh, NOW) is None

    stats = speed_stats([answered(1, removed_after=5)] * 2 + [fresh], NOW)
    assert (stats.response_samples, stats.removal_samples) == (2, 2)
    assert takedown_speed(stats) == "unknown"  # two timed notices are not a track record


def test_an_old_open_notice_counts_as_never_responded_and_never_removed() -> None:
    silent = NoticeTimes(sent_at=NOW - OPEN_NOTICE_WINDOW)  # exactly at the window
    assert time_to_response(silent, NOW) == NEVER
    assert time_to_removal(silent, NOW) == NEVER

    stats = speed_stats([answered(1, removed_after=5), silent, silent], NOW)
    assert (stats.responded, stats.response_samples) == (1, 3)
    assert stats.share_responded == pytest.approx(1 / 3)
    assert (stats.median_to_response, stats.p90_to_response) == (NEVER, NEVER)
    assert (stats.removals_timed, stats.removal_samples) == (1, 3)
    assert takedown_speed(stats) == "very_slow"  # mostly never removed


def test_decided_outcomes_settle_never_before_the_window() -> None:
    sent = NOW - timedelta(days=2)
    assert time_to_response(NoticeTimes(sent, outcome="no_response"), NOW) == NEVER
    assert time_to_removal(NoticeTimes(sent, outcome="no_response"), NOW) == NEVER
    refused = NoticeTimes(sent, responded_at=sent + 3 * H, outcome="refused")
    assert time_to_response(refused, NOW) == 3 * H
    assert time_to_removal(refused, NOW) == NEVER
    # Forwarded to the origin host: still open for a removal until the window passes.
    assert time_to_removal(NoticeTimes(sent, sent + H, outcome="forwarded"), NOW) is None


def test_an_observed_removal_wins_over_a_recorded_reply() -> None:
    sent = NOW - timedelta(days=10)
    observed = NoticeTimes(sent, sent + 2 * H, sent + 30 * H, outcome="removed")
    assert time_to_removal(observed, NOW) == 30 * H
    # Nothing observed (the notice named no page we index): the provider's "done" reply stands in.
    reported = NoticeTimes(sent, sent + 2 * H, None, outcome="removed")
    assert time_to_removal(reported, NOW) == 2 * H
    # ...and without a reply date there is nothing to time, which is not "never".
    assert time_to_removal(NoticeTimes(sent, None, None, outcome="removed"), NOW) is None
    # Observed removal without any recorded outcome still counts.
    assert time_to_removal(NoticeTimes(sent, None, sent + 5 * H), NOW) == 5 * H


def test_a_clock_skewed_stamp_is_never_a_negative_duration() -> None:
    sent = NOW - timedelta(days=1)
    notice = NoticeTimes(sent, sent - H, sent - H, outcome="removed")
    assert time_to_response(notice, NOW) == timedelta(0)
    assert time_to_removal(notice, NOW) == timedelta(0)


def stats_with_median(median: timedelta) -> SpeedStats:
    return SpeedStats(removal_samples=MIN_SPEED_SAMPLES, median_to_removal=median)


@pytest.mark.parametrize(
    ("median", "band"),
    [
        (timedelta(0), "very_fast"),
        (VERY_FAST_MAX, "very_fast"),
        (VERY_FAST_MAX + timedelta(seconds=1), "fast"),
        (FAST_MAX, "fast"),
        (FAST_MAX + timedelta(seconds=1), "moderate"),
        (MODERATE_MAX, "moderate"),
        (MODERATE_MAX + timedelta(seconds=1), "slow"),
        (SLOW_MAX, "slow"),
        (SLOW_MAX + timedelta(seconds=1), "very_slow"),
        (NEVER, "very_slow"),
    ],
)
def test_band_thresholds_at_the_edges(median: timedelta, band: str) -> None:
    assert takedown_speed(stats_with_median(median)) == band


def test_the_thresholds() -> None:
    assert (
        timedelta(hours=24),
        timedelta(hours=72),
        timedelta(days=7),
        timedelta(days=30),
    ) == (VERY_FAST_MAX, FAST_MAX, MODERATE_MAX, SLOW_MAX)


def test_below_the_minimum_sample_the_band_is_unknown_however_fast() -> None:
    fast = SpeedStats(removal_samples=MIN_SPEED_SAMPLES - 1, median_to_removal=H)
    assert takedown_speed(fast) == "unknown"


@pytest.mark.parametrize(
    ("value", "text"),
    [(None, "-"), (NEVER, "never"), (5 * H, "5 h"), (71 * H, "71 h"), (84 * H, "3.5 d")],
)
def test_format_duration(value: timedelta | None, text: str) -> None:
    assert format_duration(value) == text
