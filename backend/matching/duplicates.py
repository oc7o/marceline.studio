"""Find pages that probably show the same video, from their frame hashes. Pure, no ORM."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from matching.hamming import HashIndex, pairwise_distances

DEFAULT_MAX_DISTANCE = 31


@dataclass(frozen=True, slots=True)
class PairStats:
    """How two pages' frames compare at a given distance threshold."""

    matched_frames: int
    min_distance: int | None
    score: float


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    page_a: int
    page_b: int
    matched_frames: int
    min_distance: int
    score: float


def _score(matched_a: int, matched_b: int, frames_a: int, frames_b: int) -> tuple[int, float]:
    # A frame counts as matched if it has any partner on the other page. Using the smaller side
    # keeps the score in [0, 1] even when one page has many near-identical frames.
    matched = min(matched_a, matched_b)
    return matched, matched / min(frames_a, frames_b)


def pair_stats(
    frames_a: Sequence[bytes], frames_b: Sequence[bytes], max_distance: int = DEFAULT_MAX_DISTANCE
) -> PairStats:
    """Compare two pages directly. `min_distance` is None if either page has no frames."""
    if not frames_a or not frames_b:
        return PairStats(matched_frames=0, min_distance=None, score=0.0)
    matrix = pairwise_distances(frames_a, frames_b)
    within = matrix <= max_distance
    matched, score = _score(
        int(np.count_nonzero(within.any(axis=1))),
        int(np.count_nonzero(within.any(axis=0))),
        len(frames_a),
        len(frames_b),
    )
    return PairStats(matched_frames=matched, min_distance=int(matrix.min()), score=score)


def find_duplicate_pages(
    frames_by_page: Mapping[int, Sequence[bytes]],
    max_distance: int = DEFAULT_MAX_DISTANCE,
    min_score: float = 0.0,
) -> list[DuplicateCandidate]:
    """Ranked candidate pairs (page_a < page_b): best score first, then more matched frames,
    then smaller distance."""
    frame_ids: list[int] = []
    frame_hashes: list[bytes] = []
    frame_page: list[int] = []
    for page_id, hashes in frames_by_page.items():
        for hash_ in hashes:
            frame_ids.append(len(frame_ids))
            frame_hashes.append(hash_)
            frame_page.append(page_id)

    index = HashIndex(frame_ids, frame_hashes)
    matched: dict[tuple[int, int], tuple[set[int], set[int]]] = {}
    closest: dict[tuple[int, int], int] = {}
    for frame, neighbours in enumerate(index.query_many(frame_hashes, max_distance)):
        page = frame_page[frame]
        for other, distance in neighbours:
            other_page = frame_page[other]
            if other_page == page:
                continue
            key = (min(page, other_page), max(page, other_page))
            sides = matched.setdefault(key, (set(), set()))
            sides[0 if page == key[0] else 1].add(frame)
            closest[key] = min(distance, closest.get(key, distance))

    candidates = []
    for (page_a, page_b), (frames_on_a, frames_on_b) in matched.items():
        count, score = _score(
            len(frames_on_a),
            len(frames_on_b),
            len(frames_by_page[page_a]),
            len(frames_by_page[page_b]),
        )
        if score >= min_score:
            candidates.append(
                DuplicateCandidate(page_a, page_b, count, closest[(page_a, page_b)], score)
            )
    candidates.sort(key=lambda c: (-c.score, -c.matched_frames, c.min_distance, c.page_a, c.page_b))
    return candidates
