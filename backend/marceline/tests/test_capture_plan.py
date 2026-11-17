"""The pure capture policy (ADR 0016): how many frames, where, and under which caps.

No browser and no database: this is the rule the ADR documents, tested on its own so a change to
it is a visible change to the policy rather than a surprise in a capture run.
"""

from itertools import pairwise

import pytest

from marceline.capture.plan import (
    MIN_SPACING_S,
    CapturePlan,
    CapturePlanError,
    frame_count,
    sample_positions,
    sample_window,
)

HALF_HOUR = 1800.0


def test_positions_stay_inside_the_video_and_skip_its_edges() -> None:
    plan = CapturePlan()
    positions = sample_positions(HALF_HOUR, plan)

    assert positions[0] > plan.edge_skip_s
    assert positions[-1] < HALF_HOUR - plan.edge_skip_s


def test_positions_are_ascending_and_evenly_spaced() -> None:
    positions = sample_positions(HALF_HOUR, CapturePlan())

    gaps = [round(b - a, 3) for a, b in pairwise(positions)]
    assert positions == sorted(positions)
    assert len(set(gaps)) == 1, gaps


def test_frames_per_video_are_capped() -> None:
    """The cap is the cost control: a three-hour video costs the same as a thirty-minute one."""
    plan = CapturePlan(max_frames=5)

    assert frame_count(HALF_HOUR, plan) == 5
    assert len(sample_positions(3 * 3600.0, plan)) == 5


def test_a_long_video_gets_about_one_frame_per_minute_up_to_the_cap() -> None:
    plan = CapturePlan(max_frames=60)

    assert frame_count(HALF_HOUR, plan) == 30
    assert frame_count(2 * 3600.0, plan) == 60


def test_a_short_video_never_falls_below_the_floor() -> None:
    """Ten minutes is fewer than min_frames by the per-minute rule; the floor wins."""
    plan = CapturePlan(max_frames=60)

    assert frame_count(600.0, plan) == plan.min_frames


def test_samples_are_never_closer_together_than_the_minimum_spacing() -> None:
    """A 6 s clip cannot usefully hold 20 samples: spacing caps the count below the floor."""
    positions = sample_positions(6.0, CapturePlan())

    gaps = [b - a for a, b in pairwise(positions)]
    assert gaps and min(gaps) >= MIN_SPACING_S
    assert len(positions) < CapturePlan().min_frames


def test_a_video_too_short_for_the_edge_skip_is_inset_by_a_fraction_instead() -> None:
    start, end = sample_window(10.0, CapturePlan())

    assert 0 < start < end < 10.0


@pytest.mark.parametrize("duration", [0.0, -5.0, float("nan"), float("inf")])
def test_an_unusable_duration_is_refused(duration: float) -> None:
    with pytest.raises(CapturePlanError):
        sample_positions(duration, CapturePlan())


@pytest.mark.parametrize(
    "field",
    [
        {"max_frames": 0},
        {"max_seconds": 0.0},
        {"max_bytes": 0},
        {"timeout_s": -1.0},
        {"edge_skip_s": -1.0},
        {"frames_per_minute": 0.0},
        {"min_frames": 0},
    ],
)
def test_an_unusable_cap_is_refused(field: dict[str, float]) -> None:
    with pytest.raises(CapturePlanError):
        CapturePlan(**field)  # type: ignore[arg-type]


def test_the_plan_tells_the_browser_its_caps() -> None:
    caps = CapturePlan(max_frames=7, max_seconds=30.0, max_bytes=1024, timeout_s=2.0).as_json()

    assert caps == {"maxFrames": 7, "maxSeconds": 30.0, "maxBytes": 1024, "timeoutMs": 2000}
