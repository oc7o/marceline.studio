"""The customer-facing provider response history (ADR 0027 amendment 2026-09-17): rounded, past."""

from datetime import UTC, datetime, timedelta

import pytest

from catalog.hosting.history import (
    DEFAULT_MIN_NOTICES,
    MIN_TIMED_REMOVALS,
    REMOVAL_TIMES,
    CustomerHistory,
    ResponseHistory,
    customer_history,
    removal_time_bucket,
    response_history,
    rounded_percent,
)
from catalog.hosting.speed import OPEN_NOTICE_WINDOW, NoticeTimes

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
OLD = NOW - OPEN_NOTICE_WINDOW - timedelta(days=1)
H = timedelta(hours=1)
D = timedelta(days=1)


def removed_after(duration: timedelta) -> NoticeTimes:
    return NoticeTimes(sent_at=OLD, removed_observed_at=OLD + duration, outcome="removed")


def test_codes_are_stable_and_the_default_minimum_is_five() -> None:
    assert REMOVAL_TIMES == (
        "same_day",
        "1_plus_days",
        "3_plus_days",
        "7_plus_days",
        "14_plus_days",
        "30_plus_days",
    )
    assert DEFAULT_MIN_NOTICES == 5
    assert MIN_TIMED_REMOVALS == 3


def test_which_notices_are_observed() -> None:
    notices = [
        removed_after(2 * D),  # seen gone
        NoticeTimes(sent_at=OLD, outcome="removed"),  # provider says removed, no date: untimed
        NoticeTimes(sent_at=OLD, responded_at=OLD + 5 * H, outcome="removed"),  # timed by reply
        NoticeTimes(sent_at=NOW - D, outcome="refused"),  # settled not removed, however young
        NoticeTimes(sent_at=OLD, outcome="awaiting"),  # past the window: not removed
        NoticeTimes(sent_at=OLD, outcome="forwarded"),  # past the window, nothing seen: not removed
        NoticeTimes(sent_at=NOW - D, outcome="awaiting"),  # too young: not observed
        NoticeTimes(sent_at=NOW - D, outcome="forwarded"),  # too young: not observed
    ]

    assert response_history(notices, NOW) == ResponseHistory(
        observed=6, removed=3, removal_times=(2 * D, 5 * H)
    )


@pytest.mark.parametrize(
    ("part", "whole", "expected"),
    [
        (3, 4, 75),
        (0, 5, 0),
        (5, 5, 100),
        (1, 3, 35),  # 33.3 → 35
        (2, 3, 65),  # 66.7 → 65
        (1, 8, 15),  # 12.5 → 15: halves up
        (7, 9, 80),  # 77.8 → 80
        (19, 20, 95),
        (39, 40, 100),  # 97.5 → 100
        (6, 7, 85),  # 85.7 → 85
    ],
)
def test_the_share_is_rounded_to_the_nearest_five_percent(
    part: int, whole: int, expected: int
) -> None:
    assert rounded_percent(part, whole) == expected


def test_a_share_of_nothing_is_refused() -> None:
    with pytest.raises(ValueError):
        rounded_percent(0, 0)


@pytest.mark.parametrize(
    ("duration", "bucket"),
    [
        (timedelta(0), "same_day"),
        (23 * H, "same_day"),
        (D, "1_plus_days"),
        (3 * D - H, "1_plus_days"),  # floored, never rounded up
        (3 * D, "3_plus_days"),
        (7 * D, "7_plus_days"),
        (14 * D - H, "7_plus_days"),
        (14 * D, "14_plus_days"),
        (30 * D, "30_plus_days"),
        (400 * D, "30_plus_days"),
    ],
)
def test_the_median_is_floored_into_a_bucket(duration: timedelta, bucket: str) -> None:
    assert removal_time_bucket(duration) == bucket


def test_below_the_minimum_there_is_no_history_at_all() -> None:
    four = response_history([removed_after(H)] * 4, NOW)

    assert customer_history(four, min_notices=5) is None
    assert customer_history(ResponseHistory(), min_notices=0) is None  # nothing is never 0 %
    assert customer_history(four, min_notices=4) == CustomerHistory(100, "same_day")


def test_the_example_from_the_operator_decision() -> None:
    notices = [removed_after(3 * D), removed_after(4 * D), removed_after(5 * D)]
    notices += [NoticeTimes(sent_at=OLD, outcome="refused")]

    history = customer_history(response_history(notices, NOW), min_notices=4)

    assert history == CustomerHistory(removed_percent=75, usual_removal_time="3_plus_days")


def test_the_median_is_over_timed_removals_with_the_upper_rank() -> None:
    notices = [removed_after(H), removed_after(2 * D), removed_after(8 * D), removed_after(9 * D)]
    notices += [NoticeTimes(sent_at=OLD, outcome="no_response")] * 4

    history = customer_history(response_history(notices, NOW), min_notices=5)

    assert history == CustomerHistory(removed_percent=50, usual_removal_time="7_plus_days")


def test_too_few_dated_removals_give_a_share_without_a_time() -> None:
    notices = [removed_after(H), NoticeTimes(sent_at=OLD, outcome="removed")]
    notices += [NoticeTimes(sent_at=OLD, outcome="refused")] * 4

    history = customer_history(response_history(notices, NOW), min_notices=5)

    assert history == CustomerHistory(removed_percent=35, usual_removal_time=None)
