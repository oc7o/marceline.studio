"""Copy extent of a match: full copy, excerpt, compilation candidate or unknown (issue #62).

Pure and deterministic, no ORM. `matching.references.refresh_copy_extents` stores the result on
`ReferenceMatch` after every matching run.

**Why it exists.** Clips are leaks: an excerpt stays a match, stays in its video's cluster and
stays a customer finding — duration is an indicator, never a filter. But "excerpt = leak" is a
*matching* rule and must not flow automatically into a notice: an excerpt or a compilation needs a
licence/exception review first. So the future notice gate has to know which it is looking at, and
`requires_exception_review` is the one question it asks. The classification never hides, drops or
re-bands a match.

**What it looks at**, in this order:

1. A photo collection has no timeline and an original without a duration has nothing to measure
   against: `unknown`.
2. Other video originals (another file) with a current, non-rejected strong match on the same
   page: `compilation_candidate`. A pipeline twin of the same file is the same original and does
   not count; photo collections do not count either, because a set's stills are often frames of
   the very video it came with.
3. No verified frame order — a poster, single images, or fewer than `MIN_TIMELINE_FRAMES` frames
   in order: `unknown`, **never `excerpt`**. One still says which moment, not how much.
4. The page's own duration against the original (`page_relation`): clearly longer means the page
   carries more than this original (`compilation_candidate`); clearly shorter means it cannot
   carry all of it (`excerpt`), however wide the matched frames are spread — a highlights montage
   spans the whole original too.
5. Otherwise the matched timeline span: the first to the last in-order matched moment of the
   original, plus one sample period, over the original's duration (`coverage`). Wide enough is a
   `full_copy`; narrow with plenty of frames and no page duration to contradict it is an
   `excerpt`; everything between is `unknown`.

Thresholds were measured on the human-confirmed matches of 2026-09-17: the 8 confirmed matches
with a verified timeline (site03 preview clips, site02 hover frames) all have a matching page
duration and span 0.69-0.98 of their original; the lowest is a site03 clip whose 8 matched frames
stopped at 72 % of the video. The other 21 confirmed matches are poster or single-image evidence.

Bump `CLASSIFIER_VERSION` whenever a rule or threshold changes: rows classified by another version
count as not classified (`current_extent`) until the next matching run rewrites them.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from matching.duration import duration_tolerance

CLASSIFIER_VERSION = 1

# Frames needed in the longest in-order chain before a span means anything. Two frames always form
# a "chain"; three are the least that shows an order across the video (ADR 0015's loose-frame
# admission uses the same number).
MIN_TIMELINE_FRAMES = 3
# Timeline score below which the order is too broken to measure a span on (a remix or a mismatch).
# Every confirmed timeline measured scores >= 0.99.
MIN_TIMELINE_SCORE = 0.75
# Span needed for a full copy with no page duration to corroborate it. Hover frames skip the first
# and last minutes of a video (site02: 0.80 with 8 of 10 frames matched), so 0.8 is the widest a
# sparse full copy reliably reaches.
FULL_COPY_MIN_COVERAGE = 0.8
# Span needed when the page's duration matches the original: the duration already says "all of
# it", the span only has to agree. Lowest confirmed full copy measured: 0.69 (site03 clip).
FULL_COPY_MIN_COVERAGE_SAME_DURATION = 0.6
# Below this span, with no page duration, many in-order frames all inside one part of the original
# make an excerpt. Fewer frames could be a full copy whose other frames did not match.
EXCERPT_MAX_COVERAGE = 0.5
EXCERPT_MIN_FRAMES = 4
# A page counts as shorter only below this share of the original *and* beyond the duration
# tolerance (`matching.duration`): a copy missing a few percent (credits, an intro) is not an
# excerpt in any sense a reviewer would care about. Between the two it corroborates nothing: the
# span alone decides, at `FULL_COPY_MIN_COVERAGE`.
SHORTER_MAX_RATIO = 0.9


class CopyExtent(StrEnum):
    FULL_COPY = "full_copy"
    EXCERPT = "excerpt"
    COMPILATION_CANDIDATE = "compilation_candidate"
    UNKNOWN = "unknown"


class ExtentReason(StrEnum):
    """Why a match got its extent; a code, rendered by the staff UI."""

    NOT_CLASSIFIED = "not_classified"
    PHOTO_COLLECTION = "photo_collection"
    NO_REFERENCE_DURATION = "no_reference_duration"
    OTHER_ORIGINALS = "other_originals"
    NO_TIMELINE = "no_timeline"
    WEAK_TIMELINE = "weak_timeline"
    PAGE_LONGER = "page_longer"
    PAGE_SHORTER = "page_shorter"
    FULL_SPAN = "full_span"
    NARROW_SPAN = "narrow_span"
    INCONCLUSIVE = "inconclusive"


class PageRelation(StrEnum):
    UNKNOWN = "unknown"
    SAME = "same"
    LONGER = "longer"
    SHORTER = "shorter"
    SLIGHTLY_SHORTER = "slightly_shorter"


COPY_EXTENT_CHOICES = tuple((extent.value, extent.value.replace("_", " ")) for extent in CopyExtent)
EXTENT_REASON_CHOICES = tuple(
    (reason.value, reason.value.replace("_", " ")) for reason in ExtentReason
)
# Extents a notice may never skip the licence/exception review for.
REVIEW_REQUIRED = frozenset(
    {CopyExtent.EXCERPT, CopyExtent.COMPILATION_CANDIDATE, CopyExtent.UNKNOWN}
)
# How cautious an extent is when several rows describe one copy: the most cautious one wins.
_CAUTION = {
    CopyExtent.UNKNOWN: 0,
    CopyExtent.FULL_COPY: 1,
    CopyExtent.EXCERPT: 2,
    CopyExtent.COMPILATION_CANDIDATE: 3,
}


@dataclass(frozen=True)
class ExtentInputs:
    """Everything the classification reads; `span_s` is None without a verified timeline."""

    collection: bool
    reference_duration_s: float | None
    page_duration_s: float | None
    span_s: float | None
    timeline_score: float | None
    consistent_frames: int
    other_originals: int


@dataclass(frozen=True)
class ExtentResult:
    extent: CopyExtent
    reason: ExtentReason
    coverage: float | None


def matched_span(seconds: Iterable[float], frame_fps: float | None) -> float | None:
    """First to last matched moment plus one sample period; None without moments or a rate.

    A sample at second k stands for the period [k, k + 1/fps), so one matched sample spans one
    period, not zero.
    """
    moments = list(seconds)
    if not moments or not frame_fps or frame_fps <= 0:
        return None
    return round(max(moments) - min(moments) + 1 / frame_fps, 3)


def page_relation(page_duration_s: float | None, reference_duration_s: float) -> PageRelation:
    """The page's duration against the original's: an indicator, never a match filter."""
    if page_duration_s is None:
        return PageRelation.UNKNOWN
    tolerance = duration_tolerance(max(page_duration_s, reference_duration_s))
    if abs(page_duration_s - reference_duration_s) <= tolerance:
        return PageRelation.SAME
    if page_duration_s > reference_duration_s:
        return PageRelation.LONGER
    if page_duration_s <= reference_duration_s * SHORTER_MAX_RATIO:
        return PageRelation.SHORTER
    return PageRelation.SLIGHTLY_SHORTER


def classify(inputs: ExtentInputs) -> ExtentResult:
    """The extent of one match; see the module docstring for the order of the rules."""
    duration = inputs.reference_duration_s
    if inputs.collection:
        return ExtentResult(CopyExtent.UNKNOWN, ExtentReason.PHOTO_COLLECTION, None)
    if duration is None or duration <= 0:
        return ExtentResult(CopyExtent.UNKNOWN, ExtentReason.NO_REFERENCE_DURATION, None)
    coverage = (
        None if inputs.span_s is None else round(min(1.0, max(0.0, inputs.span_s / duration)), 4)
    )
    if inputs.other_originals > 0:
        return ExtentResult(
            CopyExtent.COMPILATION_CANDIDATE, ExtentReason.OTHER_ORIGINALS, coverage
        )
    if (
        inputs.timeline_score is None
        or coverage is None
        or inputs.consistent_frames < MIN_TIMELINE_FRAMES
    ):
        return ExtentResult(CopyExtent.UNKNOWN, ExtentReason.NO_TIMELINE, coverage)
    if inputs.timeline_score < MIN_TIMELINE_SCORE:
        return ExtentResult(CopyExtent.UNKNOWN, ExtentReason.WEAK_TIMELINE, coverage)
    relation = page_relation(inputs.page_duration_s, duration)
    if relation == PageRelation.LONGER:
        return ExtentResult(CopyExtent.COMPILATION_CANDIDATE, ExtentReason.PAGE_LONGER, coverage)
    if relation == PageRelation.SHORTER:
        return ExtentResult(CopyExtent.EXCERPT, ExtentReason.PAGE_SHORTER, coverage)
    needed = (
        FULL_COPY_MIN_COVERAGE_SAME_DURATION
        if relation == PageRelation.SAME
        else FULL_COPY_MIN_COVERAGE
    )
    if coverage >= needed:
        return ExtentResult(CopyExtent.FULL_COPY, ExtentReason.FULL_SPAN, coverage)
    if (
        relation == PageRelation.UNKNOWN
        and coverage < EXCERPT_MAX_COVERAGE
        and inputs.consistent_frames >= EXCERPT_MIN_FRAMES
    ):
        return ExtentResult(CopyExtent.EXCERPT, ExtentReason.NARROW_SPAN, coverage)
    return ExtentResult(CopyExtent.UNKNOWN, ExtentReason.INCONCLUSIVE, coverage)


class ClassifiedMatch(Protocol):
    """What a stored classification looks like (`ReferenceMatch`)."""

    @property
    def copy_extent(self) -> str: ...

    @property
    def copy_extent_version(self) -> int: ...


def current_extent(match: ClassifiedMatch | None) -> CopyExtent:
    """The stored extent; `unknown` for no match and for rows another classifier version wrote."""
    if match is None or match.copy_extent_version != CLASSIFIER_VERSION:
        return CopyExtent.UNKNOWN
    try:
        return CopyExtent(match.copy_extent)
    except ValueError:
        return CopyExtent.UNKNOWN


def extent_requires_exception_review(extent: CopyExtent) -> bool:
    return extent in REVIEW_REQUIRED


def requires_exception_review(match: ClassifiedMatch | None) -> bool:
    """For the future notice gate: must a licence/exception review happen before any notice?

    True for an excerpt, a compilation candidate and anything not determined — including no match
    at all and a row the current classifier has not written. Only a current `full_copy` is false,
    and even then every other gate of the notice flow still applies.
    """
    return extent_requires_exception_review(current_extent(match))


def caution(extent: CopyExtent) -> int:
    """How cautious an extent is; the higher value wins in `combine_extents`."""
    return _CAUTION[extent]


def combine_extents(extents: Sequence[CopyExtent]) -> CopyExtent:
    """One extent for rows describing the same copy (one file's pipeline twins on one page).

    The most cautious determined value wins: a compilation candidate over an excerpt over a full
    copy; `unknown` only when nothing was determined.
    """
    return max(extents, key=caution, default=CopyExtent.UNKNOWN)
