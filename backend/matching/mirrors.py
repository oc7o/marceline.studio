"""Does mirroring actually happen in our index? (issue #8) — read-only, no requests, no writes.

Page frames are hashed in one orientation; references are hashed in all eight. That arrangement
already answers a mirrored *page* against an *original* we own, because the reference's own
mirrored variant is in the search. It answers nothing where no reference is involved: duplicates,
clusters and common frames compare page hash with page hash, both unflipped.

Two questions, and this module measures both from stored hashes alone:

1. **Does the reference-side expansion ever earn its keep?** A page frame whose only reference hit
   is on a flipped orientation is a mirrored page frame, observed. Zero of them over the whole
   index is the evidence issue #8 asks for that mirroring is not a route the sites take.
2. **What would the page-vs-page side gain?** Every page frame is mirrored with
   `pdq.dihedral_from_hash` — an approximation, but the only one that is retroactive over hashes
   whose pixels are gone — and searched against the page index. A pair the mirror finds and the
   plain search does not is a duplicate/cluster edge we are missing today.

Nothing here writes, fetches or names a URL or a title: page ids, site ids and counts only.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np
from django.db.models import Count
from numpy.typing import NDArray

from index.models import FrameHash, Page
from matching.hamming import HashIndex, to_words
from matching.models import ClusterMember, ReferenceFrameHash
from matching.pdq import (
    DIHEDRAL_ORIENTATIONS,
    MIN_QUALITY,
    MIRROR_ORIENTATION,
    ORIENTATION_NAMES,
    dihedral_from_hash,
)
from matching.reference_hashes import load_reference_hashes
from matching.references import LOOSE_MAX_DISTANCE, STRICT_MAX_DISTANCE

UNFLIPPED = 0
# How many ids a line may name before it becomes a list rather than a pointer.
MAX_IDS = 20


@dataclass
class ReferenceCoverage:
    """How many reference hashes exist per orientation, and who has no expansion at all."""

    per_orientation: dict[int, int] = field(default_factory=dict)
    references_expanded: int = 0
    references_unflipped_only: list[int] = field(default_factory=list)

    def lines(self) -> list[str]:
        total = sum(self.per_orientation.values())
        out = [f"Reference hashes: {total} over {len(self.per_orientation)} orientations"]
        for orientation in range(DIHEDRAL_ORIENTATIONS):
            count = self.per_orientation.get(orientation, 0)
            out.append(f"  {orientation} {ORIENTATION_NAMES[orientation]:6s} {count}")
        missing = sorted(self.references_unflipped_only)
        line = (
            f"References with the dihedral expansion: {self.references_expanded}; "
            f"unflipped only: {len(missing)}"
        )
        if missing:
            line += f" (ids {missing[:MAX_IDS]})"
        out.append(line)
        return out


@dataclass
class OrientationWins:
    """Which reference orientation each page frame's nearest reference hash sits on."""

    frames_searched: int = 0
    frames_with_a_hit: int = 0
    best_orientation: Counter[int] = field(default_factory=Counter)
    only_flipped: Counter[int] = field(default_factory=Counter)
    only_flipped_pages: set[int] = field(default_factory=set)

    def lines(self) -> list[str]:
        out = [
            f"Page frames searched: {self.frames_searched}; within the radius: "
            f"{self.frames_with_a_hit}",
            "Nearest reference hash by orientation:",
        ]
        for orientation in range(DIHEDRAL_ORIENTATIONS):
            count = self.best_orientation.get(orientation, 0)
            tail = (
                "" if orientation == UNFLIPPED else f", sole hit: {self.only_flipped[orientation]}"
            )
            out.append(f"  {orientation} {ORIENTATION_NAMES[orientation]:6s} {count}{tail}")
        pages = sorted(self.only_flipped_pages)
        line = (
            "Page frames matched ONLY by a flipped reference orientation: "
            f"{sum(self.only_flipped.values())} on {len(pages)} pages"
        )
        if pages:
            line += f" (page ids {pages[:MAX_IDS]})"
        out.append(line)
        return out


@dataclass
class MirroredPagePairs:
    """Page pairs the derived mirror finds and the plain page-vs-page search does not."""

    pages: int = 0
    plain_pairs: int = 0
    mirror_only_pairs: int = 0
    mirror_only_cross_site: int = 0
    mirror_only_unclustered: int = 0
    examples: list[tuple[int, int, int]] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"Active pages with usable frames: {self.pages}",
            f"Page pairs within the radius today: {self.plain_pairs}",
            f"Page pairs only the derived mirror finds: {self.mirror_only_pairs} "
            f"(cross-site {self.mirror_only_cross_site}, not already one cluster "
            f"{self.mirror_only_unclustered})",
        ]
        if self.examples:
            out.append("  nearest of them (page a, page b, distance):")
            out.extend(f"    {a} {b} {distance}" for a, b, distance in self.examples)
        return out


def reference_coverage() -> ReferenceCoverage:
    """Counts per orientation straight from the database — is the expansion really stored?"""
    coverage = ReferenceCoverage()
    rows = (
        ReferenceFrameHash.objects.values("dihedral")
        .order_by("dihedral")
        .annotate(total=Count("pk"))
    )
    coverage.per_orientation = {int(row["dihedral"]): int(row["total"]) for row in rows}
    seen: dict[int, set[int]] = defaultdict(set)
    for reference_id, dihedral in (
        ReferenceFrameHash.objects.values_list("reference_id", "dihedral").distinct().iterator()
    ):
        seen[int(reference_id)].add(int(dihedral))
    for reference_id, orientations in seen.items():
        if orientations == {UNFLIPPED}:
            coverage.references_unflipped_only.append(reference_id)
        else:
            coverage.references_expanded += 1
    return coverage


def page_frames(min_quality: int = MIN_QUALITY) -> tuple[list[int], list[bytes]]:
    """(page ids, hashes) of quality-eligible frames of active pages, one entry per frame."""
    page_ids: list[int] = []
    hashes: list[bytes] = []
    for page_id, pdq in (
        FrameHash.objects.filter(quality__gte=min_quality, media__page__active=True)
        .order_by("pk")
        .values_list("media__page_id", "pdq")
        .iterator(chunk_size=5000)
    ):
        page_ids.append(int(page_id))
        hashes.append(bytes(pdq))
    return page_ids, hashes


def orientation_wins(
    page_ids: list[int],
    page_words: NDArray[np.uint64],
    max_distance: int = LOOSE_MAX_DISTANCE,
    min_quality: int = MIN_QUALITY,
) -> OrientationWins:
    """For every page frame, which reference orientation its nearest reference hash sits on.

    A frame counted in `only_flipped` has **no** unflipped reference hash within the radius at
    all: it is a page frame the dihedral expansion is the sole reason we found.
    """
    wins = OrientationWins(frames_searched=len(page_ids))
    snapshot = load_reference_hashes()
    eligible = snapshot.eligible(min_quality)
    if not len(page_ids) or not eligible.any():
        return wins
    rows = np.flatnonzero(eligible)
    index = HashIndex.from_words(rows, snapshot.words[eligible])
    positions, hits, distances = index.pairs_within(page_words, max_distance)
    if not len(positions):
        return wins
    orientations = snapshot.dihedral[hits]
    best: dict[int, tuple[int, int]] = {}
    unflipped: set[int] = set()
    for position, orientation, distance in zip(
        positions.tolist(), orientations.tolist(), distances.tolist(), strict=True
    ):
        current = best.get(position)
        if current is None or distance < current[0]:
            best[position] = (distance, orientation)
        if orientation == UNFLIPPED:
            unflipped.add(position)
    wins.frames_with_a_hit = len(best)
    for position, (_, orientation) in best.items():
        wins.best_orientation[orientation] += 1
        if orientation != UNFLIPPED and position not in unflipped:
            wins.only_flipped[orientation] += 1
            wins.only_flipped_pages.add(page_ids[position])
    return wins


def mirrored_page_pairs(
    page_ids: list[int],
    hashes: list[bytes],
    max_distance: int = STRICT_MAX_DISTANCE,
    limit: int = MAX_IDS,
) -> MirroredPagePairs:
    """Page pairs found only when one side's hashes are mirrored (derived, no pixels)."""
    result = MirroredPagePairs(pages=len(set(page_ids)))
    if not hashes:
        return result
    pages = np.asarray(page_ids, dtype=np.int64)
    index = HashIndex(list(range(len(hashes))), hashes)
    plain_first, plain_second, _ = index.neighbor_pairs(max_distance)
    plain = {
        pair
        for pair in zip(
            np.minimum(pages[plain_first], pages[plain_second]).tolist(),
            np.maximum(pages[plain_first], pages[plain_second]).tolist(),
            strict=True,
        )
        if pair[0] != pair[1]
    }
    result.plain_pairs = len(plain)
    mirrored = to_words([dihedral_from_hash(value, MIRROR_ORIENTATION) for value in hashes])
    positions, hits, distances = index.pairs_within(mirrored, max_distance)
    found: dict[tuple[int, int], int] = {}
    for position, hit, distance in zip(
        positions.tolist(), hits.tolist(), distances.tolist(), strict=True
    ):
        first, second = int(pages[position]), int(pages[hit])
        if first == second:
            continue
        key = (min(first, second), max(first, second))
        if key in plain:
            continue
        if distance < found.get(key, max_distance + 1):
            found[key] = distance
    result.mirror_only_pairs = len(found)
    if not found:
        return result
    involved = {page for pair in found for page in pair}
    site_of = dict(Page.objects.filter(pk__in=involved).values_list("pk", "site_id"))
    clusters: dict[int, set[int]] = defaultdict(set)
    for page_id, cluster_id in (
        ClusterMember.objects.filter(page_id__in=involved)
        .exclude(status=ClusterMember.Status.REJECTED)
        .values_list("page_id", "cluster_id")
    ):
        clusters[int(page_id)].add(int(cluster_id))
    for first, second in found:
        if site_of.get(first) != site_of.get(second):
            result.mirror_only_cross_site += 1
        if not clusters[first] & clusters[second]:
            result.mirror_only_unclustered += 1
    result.examples = sorted(
        ((first, second, distance) for (first, second), distance in found.items()),
        key=lambda item: (item[2], item[0], item[1]),
    )[:limit]
    return result


def mirror_report(
    max_distance: int = STRICT_MAX_DISTANCE,
    reference_distance: int = LOOSE_MAX_DISTANCE,
    min_quality: int = MIN_QUALITY,
    limit: int = MAX_IDS,
) -> list[str]:
    """The whole measurement as plain text. Read-only; writes nothing, requests nothing."""
    lines = ["Mirrored frames in the index (issue #8) - read-only", ""]
    lines.extend(reference_coverage().lines())
    lines.append("")
    page_ids, hashes = page_frames(min_quality)
    lines.append(
        f"Reference side, radius {reference_distance}: does a flipped orientation ever win?"
    )
    lines.extend(
        orientation_wins(page_ids, to_words(hashes), reference_distance, min_quality).lines()
    )
    lines.append("")
    lines.append(f"Page vs page, radius {max_distance}: what the derived mirror would add")
    lines.extend(mirrored_page_pairs(page_ids, hashes, max_distance, limit).lines())
    return lines
