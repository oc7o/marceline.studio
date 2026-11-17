"""Batched searches (M3/RE2), the reference hash snapshot (RE3) and FAISS thread caps (RE5).

The batched functions must give exactly what the per-query loops they replaced gave; the loops
are kept here as the oracle.
"""

import hashlib
from collections import defaultdict
from collections.abc import Hashable

import numpy as np
import pytest
from django.db.models import Count, Max
from pytest_django.fixtures import Settings

from catalog.models import Creator
from matching import ann
from matching.hamming import HashIndex
from matching.models import Reference, ReferenceFrameHash
from matching.reference_hashes import load_reference_hashes, signatures
from matching.references import (
    STRICT_MAX_DISTANCE,
    FrameMatch,
    PageEvidence,
    PageFrame,
    ReferenceFrame,
    SharedCandidate,
    aggregate_matches,
    find_shared_frames,
    mark_shared_frames,
)
from matching.tests.hashes import flip_bits, random_hashes
from matching.timeline import FramePosition, Ordering


def reference_set() -> list[SharedCandidate]:
    scenes = random_hashes(60, seed=3)
    intro = random_hashes(1, seed=4)[0]
    frames: list[SharedCandidate] = []
    for rid in range(1, 7):
        digest = "same" if rid in (5, 6) else f"file-{rid}"
        for index in range(10):
            value = scenes[(rid - 1) * 10 + index]
            if index == 0:
                value = flip_bits(intro, rid, seed=rid)
            if rid == 6:  # a second pipeline of reference 5's file: near-identical grid
                value = flip_bits(scenes[40 + index], 3, seed=index)
            frames.append(SharedCandidate(rid, digest, frozenset({rid}), index, 0, value))
            frames.append(
                SharedCandidate(
                    rid, digest, frozenset({rid}), index, 1, bytes(255 - b for b in value)
                )
            )
    return frames


def page_set(frames: list[SharedCandidate]) -> list[tuple[int, bytes]]:
    pages = [(100 + i, value) for i, value in enumerate(random_hashes(40, seed=5))]
    for page in range(8):  # one scene frame on eight pages: common footage
        pages.append((200 + page, flip_bits(frames[4].pdq, 5 + page, seed=page)))
    pages.append((3, flip_bits(frames[30].pdq, 12)))  # a page linked to reference 2's creator
    return pages


def old_find_shared_frames(
    reference_frames: list[SharedCandidate],
    page_frames: list[tuple[int, bytes]],
    page_groups: dict[int, Hashable],
    page_creators: dict[int, set[int]],
    max_distance: int = STRICT_MAX_DISTANCE,
    common_page_groups: int = 5,
) -> set[tuple[int, int]]:
    shared: set[tuple[int, int]] = set()
    originals = [frame for frame in reference_frames if frame.dihedral == 0]
    index = HashIndex.from_pairs((i, frame.pdq) for i, frame in enumerate(originals))
    for frame in originals:
        for i, _ in index.query(frame.pdq, max_distance):
            other = originals[i]
            if other.reference_id != frame.reference_id and other.sha256 != frame.sha256:
                shared.add((frame.reference_id, frame.frame_index))
    everything = HashIndex.from_pairs((i, frame.pdq) for i, frame in enumerate(reference_frames))
    groups: dict[tuple[int, int], set[Hashable]] = defaultdict(set)
    for page_id, value in page_frames:
        for i, _ in everything.query(value, max_distance):
            frame = reference_frames[i]
            if frame.creator_ids.isdisjoint(page_creators.get(page_id, ())):
                groups[(frame.reference_id, frame.frame_index)].add(
                    page_groups.get(page_id, ("page", page_id))
                )
    shared.update(key for key, seen in groups.items() if len(seen) >= common_page_groups)
    return shared


@pytest.mark.parametrize("faiss_size", [ann.MIN_FAISS_SIZE, 1])
def test_batched_shared_frames_equal_the_per_query_loop(
    monkeypatch: pytest.MonkeyPatch, faiss_size: int
) -> None:
    monkeypatch.setattr(ann, "MIN_FAISS_SIZE", faiss_size)  # 1: every index goes through FAISS
    frames = reference_set()
    pages = page_set(frames)
    groups: dict[int, Hashable] = {200: ("cluster", 1), 201: ("cluster", 1)}
    creators = {3: {2}}
    expected = old_find_shared_frames(frames, pages, groups, creators)
    assert (1, 0) in expected and (2, 0) in expected  # the intro
    assert not {key for key in expected if key[0] in (5, 6) and key[1] > 0}  # same file exempt
    assert find_shared_frames(frames, pages, groups, creators) == expected
    assert find_shared_frames(frames, pages, groups, creators, common_page_groups=8) == (
        old_find_shared_frames(frames, pages, groups, creators, common_page_groups=8)
    )
    assert find_shared_frames([], pages, {}, {}) == set()


def old_aggregate(
    reference_frames: dict[int, ReferenceFrame], page_frames: list[PageFrame]
) -> dict[tuple[int, int], PageEvidence]:
    index = HashIndex.from_pairs((pk, row.pdq) for pk, row in reference_frames.items())
    evidence: dict[tuple[int, int], PageEvidence] = {}
    for key, frame in enumerate(page_frames):
        for pk, distance in index.query(frame.pdq, 40):
            row = reference_frames[pk]
            if row.shared and distance > STRICT_MAX_DISTANCE:
                continue
            entry = evidence.setdefault((row.reference_id, frame.page_id), PageEvidence())
            hits = entry.frames.setdefault(key, FrameMatch(frame.position, frame.common))
            (hits.shared if row.shared else hits.clean).append((distance, row.timestamp_s))
    for entry in evidence.values():
        entry.resolve()
    return evidence


@pytest.mark.parametrize("faiss_size", [ann.MIN_FAISS_SIZE, 1])
def test_batched_aggregation_equals_the_per_query_loop(
    monkeypatch: pytest.MonkeyPatch, faiss_size: int
) -> None:
    monkeypatch.setattr(ann, "MIN_FAISS_SIZE", faiss_size)
    scenes = random_hashes(30, seed=9)
    reference_frames = {
        pk: ReferenceFrame(1 + pk % 3, float(pk // 3), pk % 7 == 0, value)
        for pk, value in enumerate(scenes)
    }
    page_frames = []
    for i, value in enumerate(scenes[:20]):
        position = FramePosition("hover:/a", i, Ordering.HOVER) if i % 2 else None
        page_frames.append(PageFrame(50 + i % 4, flip_bits(value, 4 + (i * 7) % 36, seed=i), i))
        page_frames.append(PageFrame(60, flip_bits(value, 33, seed=i), 100 + i, position))
    evidence, totals = aggregate_matches(reference_frames, page_frames)
    expected = old_aggregate(reference_frames, page_frames)
    assert list(evidence) == list(expected)
    for key, entry in expected.items():
        got = evidence[key]
        assert got.frames == entry.frames
        assert (got.admitted, got.timeline) == (entry.admitted, entry.timeline)
    assert totals[60] == 20


def test_search_threads_are_capped(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert ann.search_threads(1) == 1  # a small batch never pays for thread start-up
    assert ann.search_threads(ann.SMALL_BATCH) == ann.DEFAULT_THREADS == 2
    settings.MATCHING_FAISS_THREADS = 3
    assert ann.search_threads(10_000) == 3
    settings.MATCHING_FAISS_THREADS = "bogus"
    assert ann.search_threads(10_000) == 2
    del settings.MATCHING_FAISS_THREADS
    monkeypatch.setenv("MATCHING_FAISS_THREADS", "0")
    assert ann.search_threads(10_000) == 1


@pytest.mark.django_db
def test_snapshot_signatures_and_shared_flags_in_place() -> None:
    creator = Creator.objects.create(name="Snapshot Creator")
    intro, first, second = random_hashes(3, seed=21)
    references = []
    for title, scene in (("A", first), ("B", second)):
        reference = Reference.objects.create(
            creator=creator,
            title=title,
            sha256=hashlib.sha256(title.encode()).hexdigest(),
            frame_fps=1,
        )
        for index, (value, quality) in enumerate(((intro, 100), (scene, 100), (scene, 10))):
            ReferenceFrameHash.objects.create(
                reference=reference,
                frame_index=index,
                timestamp_s=index,
                pdq=value,
                quality=quality,
            )
        references.append(reference)
    empty = Reference.objects.create(
        creator=creator, title="C", sha256=hashlib.sha256(b"C").hexdigest(), frame_fps=1
    )

    hashes = load_reference_hashes()
    assert len(hashes) == 6 and hashes.words.shape == (6, 4)
    assert bytes(hashes.words[0].tobytes()) == intro
    expected = {str(pk): [0, 0] for pk in Reference.objects.values_list("pk", flat=True)}
    for row in (
        ReferenceFrameHash.objects.filter(quality__gte=50)
        .values("reference_id")
        .annotate(frames=Count("pk"), last=Max("pk"))
    ):
        expected[str(row["reference_id"])] = [row["frames"], row["last"]]
    assert signatures(hashes, [r.pk for r in (*references, empty)], 50) == expected

    assert mark_shared_frames(hashes=hashes) == 2
    stored = dict(ReferenceFrameHash.objects.values_list("pk", "shared"))
    assert dict(zip(hashes.pk.tolist(), hashes.shared.tolist(), strict=True)) == stored
    assert np.count_nonzero(hashes.shared) == 2
