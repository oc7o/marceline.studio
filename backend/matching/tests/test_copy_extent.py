"""Copy extent (issue #62): pure classification on synthetic timelines, the notice-gate helper."""

from dataclasses import dataclass, replace

import pytest

from matching.copy_extent import (
    CLASSIFIER_VERSION,
    EXCERPT_MAX_COVERAGE,
    FULL_COPY_MIN_COVERAGE,
    FULL_COPY_MIN_COVERAGE_SAME_DURATION,
    CopyExtent,
    ExtentInputs,
    ExtentReason,
    PageRelation,
    classify,
    combine_extents,
    current_extent,
    matched_span,
    page_relation,
    requires_exception_review,
)
from matching.timeline import FramePosition, Ordering, TimelineFrame, verify_timeline

ORIGINAL_S = 3600.0


def hover_frames(seconds: list[float]) -> list[TimelineFrame]:
    """Hover frames `_1.jpg`…`_N.jpg` of one page matching these original seconds, in order."""
    return [
        TimelineFrame(i, FramePosition("hover:/p", i + 1, Ordering.HOVER), (second,))
        for i, second in enumerate(seconds)
    ]


def inputs_for(
    seconds: list[float],
    *,
    page_duration_s: float | None = ORIGINAL_S,
    other_originals: int = 0,
    fps: float = 1.0,
) -> ExtentInputs:
    """Classification inputs the way matching derives them: a verified timeline, then its span."""
    timeline = verify_timeline(hover_frames(seconds))
    return ExtentInputs(
        collection=False,
        reference_duration_s=ORIGINAL_S,
        page_duration_s=page_duration_s,
        span_s=matched_span(timeline.chain_seconds, fps),
        timeline_score=timeline.score,
        consistent_frames=timeline.consistent_frames,
        other_originals=other_originals,
    )


def spread(start: float, stop: float, count: int) -> list[float]:
    step = (stop - start) / (count - 1)
    return [float(round(start + step * i)) for i in range(count)]


def test_matched_span_counts_one_sample_period() -> None:
    assert matched_span([10.0, 20.0, 15.0], 1.0) == 11.0
    assert matched_span([10.0], 2.0) == 0.5
    assert matched_span([], 1.0) is None
    assert matched_span([10.0], None) is None


def test_chain_seconds_follow_the_in_order_chain_only() -> None:
    # The third frame jumps back to the intro: not in the chain, so it cannot widen the span.
    frames = hover_frames([600, 1200, 5, 1800, 2400])
    timeline = verify_timeline(frames)
    assert timeline.chain_seconds == (600, 1200, 1800, 2400)
    assert matched_span(timeline.chain_seconds, 1.0) == 1801


def test_hover_frames_across_the_whole_original_are_a_full_copy() -> None:
    result = classify(inputs_for(spread(300, 3300, 10)))
    assert result.extent == CopyExtent.FULL_COPY
    assert result.reason == ExtentReason.FULL_SPAN
    assert result.coverage == pytest.approx(3001 / ORIGINAL_S, abs=1e-4)


def test_a_matching_page_duration_lowers_the_span_a_full_copy_needs() -> None:
    # 0.69 of the original, like the lowest confirmed site03 clip measured.
    seconds = spread(60, 2540, 8)
    assert FULL_COPY_MIN_COVERAGE_SAME_DURATION < 2481 / ORIGINAL_S < FULL_COPY_MIN_COVERAGE
    assert classify(inputs_for(seconds)).extent == CopyExtent.FULL_COPY
    unknown_page = classify(inputs_for(seconds, page_duration_s=None))
    assert (unknown_page.extent, unknown_page.reason) == (
        CopyExtent.UNKNOWN,
        ExtentReason.INCONCLUSIVE,
    )


def test_a_clearly_shorter_page_is_an_excerpt_even_when_frames_span_the_original() -> None:
    # A ten-minute highlights montage of a one-hour video spans all of it and is still an excerpt.
    montage = classify(inputs_for(spread(100, 3500, 10), page_duration_s=600))
    assert (montage.extent, montage.reason) == (CopyExtent.EXCERPT, ExtentReason.PAGE_SHORTER)
    part = classify(inputs_for(spread(0, 880, 6), page_duration_s=900))
    assert (part.extent, part.reason) == (CopyExtent.EXCERPT, ExtentReason.PAGE_SHORTER)


def test_a_few_minutes_missing_is_not_an_excerpt() -> None:
    slightly = 0.93 * ORIGINAL_S
    assert page_relation(slightly, ORIGINAL_S) == PageRelation.SLIGHTLY_SHORTER
    assert page_relation(ORIGINAL_S - 60, ORIGINAL_S) == PageRelation.SAME
    assert classify(inputs_for(spread(200, 3400, 10), page_duration_s=slightly)).extent == (
        CopyExtent.FULL_COPY
    )
    narrow = classify(inputs_for(spread(0, 600, 6), page_duration_s=slightly))
    assert narrow.extent == CopyExtent.UNKNOWN


def test_a_narrow_span_without_page_duration_is_an_excerpt_only_with_enough_frames() -> None:
    narrow = spread(1200, 1800, 5)
    assert 601 / ORIGINAL_S < EXCERPT_MAX_COVERAGE
    result = classify(inputs_for(narrow, page_duration_s=None))
    assert (result.extent, result.reason) == (CopyExtent.EXCERPT, ExtentReason.NARROW_SPAN)
    # Three frames in one part of the video may be a full copy whose other frames did not match.
    assert classify(inputs_for(narrow[:3], page_duration_s=None)).extent == CopyExtent.UNKNOWN
    # A duration saying "all of it" contradicts the narrow span: not determined.
    assert classify(inputs_for(narrow)).extent == CopyExtent.UNKNOWN


def test_a_longer_page_or_other_originals_make_a_compilation_candidate() -> None:
    longer = classify(inputs_for(spread(0, 3500, 10), page_duration_s=3 * ORIGINAL_S))
    assert (longer.extent, longer.reason) == (
        CopyExtent.COMPILATION_CANDIDATE,
        ExtentReason.PAGE_LONGER,
    )
    other = classify(inputs_for(spread(0, 3500, 10), other_originals=2))
    assert (other.extent, other.reason) == (
        CopyExtent.COMPILATION_CANDIDATE,
        ExtentReason.OTHER_ORIGINALS,
    )


def test_poster_only_evidence_is_never_an_excerpt() -> None:
    # A single still of a clearly shorter page: which moment, not how much of the video.
    timeline = verify_timeline([TimelineFrame(1, None, (1200.0,))])
    assert timeline.score is None
    poster = ExtentInputs(
        collection=False,
        reference_duration_s=ORIGINAL_S,
        page_duration_s=300,
        span_s=matched_span(timeline.chain_seconds, 1.0),
        timeline_score=timeline.score,
        consistent_frames=timeline.consistent_frames,
        other_originals=0,
    )
    result = classify(poster)
    assert (result.extent, result.reason, result.coverage) == (
        CopyExtent.UNKNOWN,
        ExtentReason.NO_TIMELINE,
        None,
    )
    # Two ordered frames are an order, not yet a span to judge.
    two = classify(inputs_for([100, 3400], page_duration_s=300))
    assert (two.extent, two.reason) == (CopyExtent.UNKNOWN, ExtentReason.NO_TIMELINE)


def test_broken_order_collections_and_missing_durations_are_unknown() -> None:
    broken = classify(replace(inputs_for(spread(0, 3500, 10)), timeline_score=0.5))
    assert (broken.extent, broken.reason) == (CopyExtent.UNKNOWN, ExtentReason.WEAK_TIMELINE)
    base = inputs_for(spread(0, 3500, 10))
    collection = classify(replace(base, collection=True))
    assert (collection.extent, collection.reason) == (
        CopyExtent.UNKNOWN,
        ExtentReason.PHOTO_COLLECTION,
    )
    no_duration = classify(replace(base, reference_duration_s=None))
    assert no_duration.reason == ExtentReason.NO_REFERENCE_DURATION
    # A span longer than the stated duration is clamped, never above 1.
    assert classify(replace(base, span_s=2 * ORIGINAL_S)).coverage == 1.0


@dataclass
class Row:
    copy_extent: str
    copy_extent_version: int = CLASSIFIER_VERSION


def test_only_a_current_full_copy_skips_the_exception_review() -> None:
    assert requires_exception_review(Row("full_copy")) is False
    for extent in ("excerpt", "compilation_candidate", "unknown", "garbage"):
        assert requires_exception_review(Row(extent)) is True
    assert requires_exception_review(None) is True
    stale = Row("full_copy", copy_extent_version=CLASSIFIER_VERSION - 1)
    assert current_extent(stale) == CopyExtent.UNKNOWN
    assert requires_exception_review(stale) is True


def test_combining_rows_of_one_copy_keeps_the_most_cautious_determined_extent() -> None:
    full, excerpt, unknown = CopyExtent.FULL_COPY, CopyExtent.EXCERPT, CopyExtent.UNKNOWN
    assert combine_extents([]) == unknown
    assert combine_extents([unknown, full]) == full
    assert combine_extents([full, excerpt, unknown]) == excerpt
    assert combine_extents([excerpt, CopyExtent.COMPILATION_CANDIDATE]) == (
        CopyExtent.COMPILATION_CANDIDATE
    )
