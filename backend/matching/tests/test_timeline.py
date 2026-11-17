import pytest

from matching.references import (
    STRICT_MAX_DISTANCE,
    FrameMatch,
    PageFrame,
    ReferenceFrame,
    aggregate_matches,
)
from matching.tests.hashes import flip_bits, random_hashes
from matching.timeline import (
    MAX_CANDIDATES_PER_FRAME,
    FramePosition,
    Ordering,
    TimelineFrame,
    frame_position,
    verify_timeline,
)


def hover(index: int, *seconds: float) -> TimelineFrame:
    return TimelineFrame(index, FramePosition("hover:/imgs/a", index, Ordering.HOVER), seconds)


def clip(index: int, *seconds: float) -> TimelineFrame:
    return TimelineFrame(index, FramePosition("clip:7", index, Ordering.CLIP), seconds)


def test_frame_position_from_media() -> None:
    assert frame_position("preview", "https://cdn.test/v.mp4", 4, 7) == FramePosition(
        "clip:7", 4, Ordering.CLIP
    )
    # Sprite tiles (issue #14) are a row-major sample across the video, exactly like a clip's.
    assert frame_position("sprite", "https://cdn.test/sprite.jpg", 9, 7) == FramePosition(
        "clip:7", 9, Ordering.CLIP
    )
    hover_frame = frame_position("frame", "https://cdn.test/imgs/34/99/abc_10.jpg?x=1", 0, 3)
    assert hover_frame == FramePosition("hover:/imgs/34/99/abc", 10, Ordering.HOVER)
    assert frame_position("poster", "https://cdn.test/imgs/34/99/abc_1.jpg", 0, 3) is None
    assert frame_position("frame", "https://cdn.test/imgs/abc_main.jpg", 0, 3) is None
    # A photo post has no position: it depicts itself, not a moment of a video (issue #69). The
    # same URL as a hover frame would have one, so this pins the kind, not the file name.
    photo_url = "https://n1.site11.example/content/a/b/sample/1000/sample_040.jpg"
    assert frame_position("photo", photo_url, 0, 3) is None
    assert frame_position("frame", photo_url, 0, 3) == FramePosition(
        "hover:/content/a/b/sample/1000/sample", 40, Ordering.HOVER
    )


def test_ordered_hover_frames_pass() -> None:
    result = verify_timeline([hover(i, 300.0 * i) for i in range(1, 11)])
    assert result.score == 1.0
    assert result.consistent_frames == 10 and result.consistent
    assert "hover: 10/10 frames in order" in result.reason


def test_shuffled_hover_frames_fail() -> None:
    seconds = [900, 100, 700, 300, 1000, 200, 600, 400, 800, 500]
    result = verify_timeline([hover(i + 1, s) for i, s in enumerate(seconds)])
    assert result.score is not None and result.score < 0.6
    assert not result.consistent


def test_small_number_of_outliers_allowed() -> None:
    seconds = [100, 200, 3000, 400, 500, 600, 700, 800]
    result = verify_timeline([hover(i + 1, s) for i, s in enumerate(seconds)])
    assert result.consistent_frames == 7 and result.consistent
    # Two outliers in four ordered frames are too many.
    assert not verify_timeline(
        [hover(1, 50), hover(2, 900), hover(3, 800), hover(4, 60)]
    ).consistent


def test_candidates_and_sampling_jitter() -> None:
    # A frame may match several reference seconds; one-second steps back are tolerated.
    result = verify_timeline([hover(1, 1013, 1012), hover(2, 1012), hover(3, 2980)])
    assert result.consistent_frames == 3


def test_nearest_candidates_are_kept_not_the_earliest() -> None:
    """A frame matching many seconds keeps its nearest MAX_CANDIDATES_PER_FRAME, in input order.

    The true moment (3000 s, listed first = nearest) is later than eight weaker hits; keeping the
    earliest eight instead dropped it and broke an otherwise perfect chain."""
    weaker = [10.0 * i for i in range(1, MAX_CANDIDATES_PER_FRAME + 1)]
    result = verify_timeline([hover(1, 1000), hover(2, 2000), hover(3, 3000, *weaker)])
    assert result.consistent_frames == 3 and result.score == 1.0


def test_frame_match_seconds_nearest_first() -> None:
    frame = FrameMatch(None, False, clean=[(30, 10.0), (5, 3000.0), (30, 20.0), (12, 10.0)])
    assert frame.nearest_seconds(31) == (3000.0, 10.0, 20.0)
    assert frame.nearest_seconds(20) == (3000.0, 10.0)
    assert frame.seconds(31) == (10.0, 20.0, 3000.0)


def test_partial_clip_passes() -> None:
    # A 4-second clip from the middle of a long reference, sampled at 2 fps.
    result = verify_timeline([clip(i, 1800 + i * 0.5) for i in range(8)])
    assert result.score == 1.0 and result.consistent
    assert "spacing R²=1.00" in result.reason
    # An evenly spaced montage (preview thumbnails) keeps a high score.
    montage = verify_timeline([clip(p, s) for p, s in [(2, 542), (5, 1626), (8, 2168), (15, 3740)]])
    assert montage.score is not None and montage.score > 0.9


def test_short_excerpt_run_passes_wherever_it_sits() -> None:
    """Verification asks for a consistent run, never for coverage (issue #73).

    A pirate page is often only an excerpt, and duration is an indicator, never a filter. Two
    frames in order anywhere in a long original are therefore enough, and a two-second run at the
    very end of a 50-minute reference scores exactly like one at the start.
    """
    shortest = verify_timeline([clip(0, 12.0), clip(1, 13.0)])
    assert shortest.score == 1.0 and shortest.consistent
    at_the_end = verify_timeline([clip(0, 2998.0), clip(1, 2999.0)])
    assert at_the_end.score == shortest.score and at_the_end.consistent
    # A run of three keeps its score whether it covers 3 s or the whole hour: no coverage term.
    tight = verify_timeline([clip(i, 1800.0 + i) for i in range(3)])
    spread = verify_timeline([clip(i, 60.0 + i * 1200) for i in range(3)])
    assert tight.score == spread.score == 1.0
    # Only our own sampled positions enter; a claimed position is never compared with anything.
    assert tight.chain_seconds == (1800.0, 1801.0, 1802.0)


def test_unordered_frames_have_no_score() -> None:
    poster = TimelineFrame(1, None, (274.0,))
    result = verify_timeline([poster])
    assert result.score is None and not result.consistent
    assert "no ordered frames" in result.reason
    assert verify_timeline([poster, hover(3, 100)]).score is None


SCENES = random_hashes(4, seed=77)
# A distance that is loose but not strict: evidence only with a timeline or
# corroboration. Derived, so it moves with the threshold instead of breaking.
LOOSE_ONLY = STRICT_MAX_DISTANCE + 2


def reference_frames() -> dict[int, ReferenceFrame]:
    return {i: ReferenceFrame(1, 100.0 * (i + 1), False, value) for i, value in enumerate(SCENES)}


def hover_frame(frame_id: int, index: int, value: bytes, page: int = 9) -> PageFrame:
    return PageFrame(page, value, frame_id, FramePosition("hover:/a", index, Ordering.HOVER))


def test_loose_distance_counts_only_with_consistent_timeline() -> None:
    in_order = [
        hover_frame(1, 1, flip_bits(SCENES[0], 10)),
        hover_frame(2, 2, flip_bits(SCENES[1], LOOSE_ONLY)),
        hover_frame(3, 3, flip_bits(SCENES[2], 12)),
    ]
    evidence, _ = aggregate_matches(reference_frames(), in_order)
    entry = evidence[(1, 9)]
    assert (entry.evidence_frames, entry.loose_frames, entry.matched_frames) == (3, 1, 3)
    assert entry.matched_seconds == [100, 200, 300]
    assert entry.timeline is not None and entry.timeline.score == 1.0

    shuffled = [
        hover_frame(1, 1, flip_bits(SCENES[2], 10)),
        hover_frame(2, 2, flip_bits(SCENES[1], LOOSE_ONLY)),
        hover_frame(3, 3, flip_bits(SCENES[0], 12)),
    ]
    entry = aggregate_matches(reference_frames(), shuffled)[0][(1, 9)]
    assert (entry.evidence_frames, entry.loose_frames) == (2, 0)
    assert entry.best_evidence == (10, 300.0)

    # Unordered loose-only hit: not a match unless corroborated.
    poster = [PageFrame(5, flip_bits(SCENES[3], LOOSE_ONLY), 10)]
    entry = aggregate_matches(reference_frames(), poster)[0][(1, 5)]
    assert entry.matched_frames == 0
    entry.resolve(corroborated=True)
    assert (entry.matched_frames, entry.evidence_frames, entry.strength) == (1, 1, "weak")
    assert entry.best_evidence == (LOOSE_ONLY, 400.0)


@pytest.mark.parametrize("distance", [41, 60])
def test_beyond_loose_distance_ignored(distance: int) -> None:
    frames = [
        hover_frame(1, 1, flip_bits(SCENES[0], 5)),
        hover_frame(2, 2, flip_bits(SCENES[1], distance)),
        hover_frame(3, 3, flip_bits(SCENES[2], 5)),
    ]
    entry = aggregate_matches(reference_frames(), frames)[0][(1, 9)]
    assert (entry.evidence_frames, entry.loose_frames) == (2, 0)
