"""Pure timeline verification of a (reference, page) match (ADR 0015).

Page frames with a known order must map to reference timestamps in the same order:
site02 hover frames `…_1.jpg`…`_10.jpg` are sampled across the video in order, and preview-clip
`frame_index` is the sample position. For clips the reference timestamps should also grow roughly
linearly with the sample position (a contiguous clip or an evenly spaced montage); hover frames
only need order. Clips cover part of the reference, so no coverage is required. Posters and other
single images have no order: the score is None, never a penalty.

A sprite sheet's tiles (issue #14) are read row by row, left to right — every real generator lays
a storyboard out in that order — so `hash_sprite`'s row-major `frame_index` is exactly the sample
position of an evenly spaced montage across the video, the same shape as a preview clip's. They
join the same CLIP ordering below rather than a kind of their own.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

HOVER_FRAME_PATTERN = re.compile(r"_(\d{1,3})\.jpe?g$", re.IGNORECASE)
# A step may go back this many reference seconds (1 fps sampling grid, near-identical shots).
TIMELINE_TOLERANCE_S = 2.0
# Ordered frames needed in one sequence before order means anything.
TIMELINE_MIN_FRAMES = 2
# Allowed outliers: floor(ratio x ordered frames), e.g. 1 of 4, 2 of 8.
TIMELINE_MAX_OUTLIER_RATIO = 0.25
# For clips, score = order x (1 - CLIP_SPACING_WEIGHT x (1 - R²)) of timestamp vs position.
CLIP_SPACING_WEIGHT = 0.25
CLIP_SPACING_MIN_POINTS = 3
# Candidate reference timestamps kept per page frame (nearest first).
MAX_CANDIDATES_PER_FRAME = 8


# Why a match has no timeline at all. Photo collections are unordered by nature (ADR 0020).
NO_ORDER_REASON = "no ordered frames (poster or single images)"
COLLECTION_REASON = "photo collection: images are independent, no timeline"


class Ordering(StrEnum):
    HOVER = "hover"
    CLIP = "clip"


@dataclass(frozen=True)
class FramePosition:
    sequence: str
    position: int
    ordering: Ordering


def frame_position(kind: str, url: str, frame_index: int, media_id: int) -> FramePosition | None:
    """Order of a page frame from its media kind and URL; None when the frame has no order."""
    # Preview clips, browser captures (ADR 0016) and sprite-sheet tiles (issue #14) are all
    # ordered samples taken across the video, so their reference timestamps should grow with the
    # sample position the same way.
    if kind in ("preview", "capture", "sprite"):
        return FramePosition(f"clip:{media_id}", frame_index, Ordering.CLIP)
    if kind == "frame":
        try:
            path = urlsplit(url).path
        except ValueError:
            return None
        match = HOVER_FRAME_PATTERN.search(path)
        if match:
            return FramePosition(
                f"hover:{path[: match.start()]}", int(match.group(1)), Ordering.HOVER
            )
    return None


@dataclass(frozen=True)
class TimelineFrame:
    """One matched page frame. `reference_seconds` are its candidate reference moments, nearest
    (smallest Hamming distance) first; only the first `MAX_CANDIDATES_PER_FRAME` distinct ones
    take part in the chain."""

    frame_id: int
    position: FramePosition | None
    reference_seconds: tuple[float, ...]


@dataclass(frozen=True)
class TimelineResult:
    score: float | None
    consistent_frames: int
    ordered_frames: int
    chain: frozenset[int]
    reason: str
    # Reference seconds the in-order chain chose, one per chain frame (all sequences together):
    # the matched timeline span of `matching.copy_extent`.
    chain_seconds: tuple[float, ...] = ()

    @property
    def consistent(self) -> bool:
        """At least TIMELINE_MIN_FRAMES in order and no more outliers than allowed."""
        if self.score is None or self.consistent_frames < TIMELINE_MIN_FRAMES:
            return False
        allowed = int(self.ordered_frames * TIMELINE_MAX_OUTLIER_RATIO)
        return self.ordered_frames - self.consistent_frames <= allowed


def no_timeline(reason: str) -> TimelineResult:
    """A result that judges nothing: score None is neutral in confidence, never a penalty."""
    return TimelineResult(None, 0, 0, frozenset(), reason)


def _longest_chain(frames: Sequence[TimelineFrame]) -> list[tuple[TimelineFrame, float]]:
    """Longest subsequence (by position) with non-decreasing reference time, one candidate per
    frame. Deterministic: ties prefer earlier positions and earlier timestamps."""
    ordered = sorted(frames, key=lambda f: (f.position.position if f.position else 0, f.frame_id))
    nodes: list[tuple[int, float]] = []  # (frame index, timestamp)
    for i, frame in enumerate(ordered):
        # Keep the *nearest* candidates (input order), then walk them in time order.
        nearest = list(dict.fromkeys(frame.reference_seconds))[:MAX_CANDIDATES_PER_FRAME]
        for second in sorted(nearest):
            nodes.append((i, second))
    best = [1] * len(nodes)
    parent: list[int | None] = [None] * len(nodes)
    for n, (i, second) in enumerate(nodes):
        for m in range(n):
            j, earlier = nodes[m]
            if j < i and earlier <= second + TIMELINE_TOLERANCE_S and best[m] + 1 > best[n]:
                best[n], parent[n] = best[m] + 1, m
    if not nodes:
        return []
    end: int | None = max(range(len(nodes)), key=lambda n: (best[n], -n))
    chain: list[tuple[TimelineFrame, float]] = []
    while end is not None:
        i, second = nodes[end]
        chain.append((ordered[i], second))
        end = parent[end]
    return chain[::-1]


def _r_squared(points: Sequence[tuple[float, float]]) -> float:
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    if sxx == 0 or syy == 0:
        return 1.0 if syy == 0 else 0.0
    if sxy <= 0:
        return 0.0
    return sxy * sxy / (sxx * syy)


def verify_timeline(frames: Sequence[TimelineFrame]) -> TimelineResult:
    """Score order (and clip spacing) of matched page frames against reference timestamps."""
    sequences: dict[str, list[TimelineFrame]] = defaultdict(list)
    for frame in frames:
        if frame.position is not None and frame.reference_seconds:
            sequences[frame.position.sequence].append(frame)
    judged = [rows for _, rows in sorted(sequences.items()) if len(rows) >= TIMELINE_MIN_FRAMES]
    if not judged:
        return no_timeline(
            "fewer than 2 ordered matched frames in one sequence" if sequences else NO_ORDER_REASON
        )
    weighted = 0.0
    consistent = ordered_total = 0
    chain_ids: set[int] = set()
    chain_seconds: list[float] = []
    reasons = []
    for rows in judged:
        chain = _longest_chain(rows)
        order = len(chain) / len(rows)
        kind = rows[0].position.ordering if rows[0].position else Ordering.HOVER
        text = f"{kind}: {len(chain)}/{len(rows)} frames in order"
        score = order
        if kind == Ordering.CLIP and len({f.position for f, _ in chain}) >= CLIP_SPACING_MIN_POINTS:
            spacing = _r_squared(
                [(float(f.position.position), s) for f, s in chain if f.position is not None]
            )
            score = order * (1 - CLIP_SPACING_WEIGHT * (1 - spacing))
            text += f", spacing R²={spacing:.2f}"
        reasons.append(text)
        weighted += score * len(rows)
        consistent += len(chain)
        ordered_total += len(rows)
        chain_ids.update(frame.frame_id for frame, _ in chain)
        chain_seconds.extend(second for _, second in chain)
    return TimelineResult(
        round(weighted / ordered_total, 4),
        consistent,
        ordered_total,
        frozenset(chain_ids),
        "; ".join(reasons),
        tuple(sorted(chain_seconds)),
    )
