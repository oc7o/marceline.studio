"""Incremental `match_references`: only new frames and references are scanned; results equal a
full run."""

import hashlib
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.utils import timezone
from pytest_django.fixtures import Settings

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from matching import ann, hamming
from matching.hamming import pairwise_distances
from matching.incremental import FRAME_OVERLAP, SCAN_VERSION, scan_candidates
from matching.models import (
    Cluster,
    ClusterMember,
    CommonFrameGroup,
    ConfidenceSettings,
    MatchRunState,
    Reference,
    ReferenceCandidate,
    ReferenceFrameHash,
    ReferenceLeak,
    ReferenceMatch,
)
from matching.references import STRICT_MAX_DISTANCE, mark_shared_frames, match_references
from matching.tests.hashes import flip_bits, random_hashes

pytestmark = pytest.mark.django_db

INTRO, SCENE_A, SCENE_B, SCENE_C, SCENE_D, SCENE_F, NOISE = random_hashes(7, seed=77)
# A distance that is loose but not strict: evidence only with a timeline or
# corroboration. Derived, so it moves with the threshold instead of breaking.
LOOSE_ONLY = STRICT_MAX_DISTANCE + 2
MATCH_FIELDS = [
    field.attname
    for field in ReferenceMatch._meta.concrete_fields
    if field.name not in {"id", "created_at", "updated_at"}
]


@pytest.fixture
def creator() -> Creator:
    return Creator.objects.create(name="Synthetic Creator")


def make_reference(creator: Creator, title: str, frames: dict[int, bytes]) -> Reference:
    reference = Reference.objects.create(
        creator=creator,
        title=title,
        sha256=hashlib.sha256(title.encode()).hexdigest(),
        frame_fps=1,
        duration_s=300,
    )
    for second, value in frames.items():
        ReferenceFrameHash.objects.create(
            reference=reference, frame_index=second, timestamp_s=second, pdq=value, quality=100
        )
    return reference


def make_page(site: Site, slug: str) -> Page:
    return Page.objects.create(site=site, title=slug, url=f"https://{site.domain}/{slug}")


def hover(page: Page, frames: dict[int, bytes]) -> None:
    """Ordered hover frames `…_N.jpg`, one media each."""
    for position, value in frames.items():
        media = Media.objects.create(
            page=page, kind="frame", url=f"https://cdn.test/{page.pk}/thumb_{position}.jpg"
        )
        FrameHash.objects.create(media=media, pdq=value, quality=100)


def poster(page: Page, value: bytes) -> FrameHash:
    media = Media.objects.create(page=page, kind="poster", url=f"https://cdn.test/{page.pk}.jpg")
    return FrameHash.objects.create(media=media, pdq=value, quality=100)


def run(**options: Any) -> list[str]:
    return match_references(Reference.objects.all(), **options)


def mode(lines: list[str]) -> str:
    assert lines[-1].startswith("Scan: ")
    return lines[-1].split()[1]


def snapshot() -> dict[str, list[Any]]:
    return {
        "matches": list(
            ReferenceMatch.objects.order_by("reference_id", "page_id").values_list(*MATCH_FIELDS)
        ),
        "leaks": list(
            ReferenceLeak.objects.order_by("reference_id", "page_id").values_list(
                "reference_id", "page_id", "status", "source", "score", "signals"
            )
        ),
        "shared": list(
            ReferenceFrameHash.objects.filter(shared=True).order_by("pk").values_list("pk")
        ),
        "candidates": list(
            ReferenceCandidate.objects.order_by("frame_id", "reference_id").values_list(
                "frame_id", "reference_id", "distance"
            )
        ),
    }


def test_incremental_run_equals_full_run(creator: Creator, site: Site) -> None:
    first = make_reference(creator, "First", {0: INTRO, 100: SCENE_A, 101: SCENE_B, 102: SCENE_C})
    make_reference(creator, "Second", {0: flip_bits(INTRO, 6), 50: SCENE_D})
    leak = make_page(site, "leak")
    hover(leak, {1: flip_bits(SCENE_A, 20)})
    loose = make_page(site, "loose")
    poster(loose, flip_bits(SCENE_A, LOOSE_ONLY))
    timeline = make_page(site, "timeline")
    hover(
        timeline,
        {1: flip_bits(SCENE_A, 12), 2: flip_bits(SCENE_B, LOOSE_ONLY), 3: flip_bits(SCENE_C, 14)},
    )
    old = make_page(site, "old")
    poster(old, flip_bits(SCENE_F, 10))
    intro = make_page(site, "intro")
    intro_frame = poster(intro, INTRO)
    studio = make_page(site, "studio")
    poster(studio, flip_bits(SCENE_D, 3))
    poster(make_page(site, "noise"), NOISE)
    CommonFrameGroup.objects.create(
        representative_hash=SCENE_D, max_distance=8, status=CommonFrameGroup.Status.CONFIRMED
    )

    assert mode(run()) == "full"
    assert not ReferenceMatch.objects.filter(page=loose).exists()
    assert ReferenceMatch.objects.get(reference=first, page=timeline).loose_frames == 1
    # Everything so far was hashed long ago.
    FrameHash.objects.update(created_at=timezone.now() - 2 * FRAME_OVERLAP)

    hover(leak, {2: flip_bits(SCENE_B, 22)})  # weak -> strong
    cluster = Cluster.objects.create()
    for page in (leak, loose):  # corroborates the loose page without new frames there
        ClusterMember.objects.create(cluster=cluster, page=page, evidence="manual")
    third = make_reference(creator, "Third", {7: SCENE_F})  # matches an old frame
    hover(make_page(site, "fresh"), {1: flip_bits(SCENE_C, 5)})
    intro_frame.delete()

    lines = run()
    assert lines[-1].startswith(
        "Scan: incremental (2 new page frames, 1 new or changed references)"
    )
    assert ReferenceMatch.objects.get(reference=first, page=leak).strength == "strong"
    assert ReferenceMatch.objects.get(reference=first, page=loose).loose_frames == 1
    assert ReferenceMatch.objects.filter(reference=third, page=old).exists()
    assert not ReferenceMatch.objects.filter(page=intro).exists()
    assert ReferenceMatch.objects.get(page=studio).common_frames == 1
    assert ReferenceLeak.objects.filter(page=loose, source="cluster").exists()
    incremental = snapshot()

    assert mode(run(full=True)) == "full"
    assert snapshot() == incremental


def test_only_scan_settings_force_a_full_scan(
    creator: Creator, site: Site, settings: Settings
) -> None:
    make_reference(creator, "First", {100: SCENE_A})
    hover(make_page(site, "leak"), {1: flip_bits(SCENE_A, 10)})

    assert mode(run()) == "full"
    assert mode(run()) == "incremental"
    # The scan radius is max(strict, loose): a stricter strict distance scans nothing new.
    assert mode(run(max_distance=30)) == "incremental"
    assert mode(run(loose_max_distance=STRICT_MAX_DISTANCE)) == "full"
    assert mode(run(min_quality=60)) == "full"
    assert mode(run()) == "full"
    assert mode(run()) == "incremental"
    state = MatchRunState.objects.get()
    assert state.settings == {
        "version": SCAN_VERSION,
        "radius": 40,
        "min_quality": 50,
        "hash_bytes": 32,
        "search": "exact",
        # ADR 0039: off, and switching it on is the one other thing that forces a full rescan.
        "crop_grid": False,
    }

    # Aggregation inputs are recomputed from the candidates on every run: no rescan needed.
    ConfidenceSettings.objects.create(pk=1, high_threshold=0.8, review_threshold=0.5)
    assert mode(run()) == "incremental"
    CommonFrameGroup.objects.create(
        representative_hash=NOISE, max_distance=8, status=CommonFrameGroup.Status.CONFIRMED
    )
    assert mode(run()) == "incremental"
    # A new reference sharing a frame changes the shared set; only that reference is scanned.
    second = make_reference(creator, "Second", {0: SCENE_A})
    mark_shared_frames()
    lines = run()
    assert lines[-1].startswith("Scan: incremental (1 new page frames, 1 new or changed")
    assert ReferenceFrameHash.objects.filter(shared=True).count() == 2
    incremental = snapshot()
    assert mode(run(full=True)) == "full"
    assert snapshot() == incremental

    # A retired reference leaves no signature behind (L5).
    second.delete()
    assert mode(run()) == "incremental"
    state = MatchRunState.objects.get()
    assert str(second.pk) not in state.reference_signatures
    assert set(state.reference_signatures) == {
        str(pk) for pk in Reference.objects.values_list("pk", flat=True)
    }
    assert ReferenceFrameHash.objects.filter(shared=True).count() == 0

    # Approximate search may miss neighbours the exact cache holds, and the other way round.
    settings.MATCHING_ANN_BACKEND = "ivf"
    expected = "full" if ann.FAISS_AVAILABLE else "incremental"
    assert mode(run()) == expected
    settings.MATCHING_ANN_BACKEND = "auto"
    assert mode(run()) == expected
    state = MatchRunState.objects.get()
    assert state.last_full_at == state.last_run_at

    out = StringIO()
    call_command("match_references", "--full", stdout=out)
    assert "Scan: full (1 page frames x 1 references)" in out.getvalue()
    out = StringIO()
    call_command("match_references", stdout=out)
    # The page frame was created within FRAME_OVERLAP of the last scan, so it is scanned again.
    assert "Scan: incremental (1 new page frames, 0 new or changed references)" in out.getvalue()


def test_changed_references_and_late_frames_are_rescanned(creator: Creator, site: Site) -> None:
    reference = make_reference(creator, "First", {100: SCENE_A})
    first = make_page(site, "first")
    poster(first, NOISE)
    assert mode(run()) == "full"
    assert not ReferenceMatch.objects.exists()
    scanned_at = MatchRunState.objects.get().frames_scanned_at
    assert scanned_at is not None
    FrameHash.objects.update(created_at=scanned_at - 2 * FRAME_OVERLAP)

    # Frames added to an existing reference: that reference is rescanned against all pages.
    ReferenceFrameHash.objects.create(
        reference=reference, frame_index=200, timestamp_s=200, pdq=NOISE, quality=100
    )
    assert run()[-1].startswith("Scan: incremental (0 new page frames, 1 new or changed")
    assert ReferenceMatch.objects.filter(page=first).exists()

    # A frame committed late (created within the overlap before the last scan) is still found.
    late = make_page(site, "late")
    last_scan = MatchRunState.objects.get().frames_scanned_at
    assert last_scan is not None
    FrameHash.objects.filter(pk=poster(late, SCENE_A).pk).update(
        created_at=last_scan - FRAME_OVERLAP / 2
    )
    # One older than the overlap is only found by a full scan.
    stale = make_page(site, "stale")
    FrameHash.objects.filter(pk=poster(stale, SCENE_A).pk).update(
        created_at=scanned_at - 3 * FRAME_OVERLAP
    )
    assert mode(run()) == "incremental"
    assert ReferenceMatch.objects.filter(page=late).exists()
    assert not ReferenceMatch.objects.filter(page=stale).exists()
    assert mode(run(full=True)) == "full"
    assert ReferenceMatch.objects.filter(page=stale).exists()


def test_scan_candidates_equals_brute_force(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hamming, "_BATCH_BUDGET_WORDS", 64)
    originals = random_hashes(24, seed=1)
    reference_hashes = [(index % 4, value) for index, value in enumerate(originals)]
    page_hashes = [
        (1000 + index, flip_bits(originals[index % 24], index % 48, seed=index))
        for index in range(60)
    ] + [(2000 + index, value) for index, value in enumerate(random_hashes(10, seed=2))]
    matrix = pairwise_distances(
        [value for _, value in page_hashes], [value for _, value in reference_hashes]
    )
    expected: dict[tuple[int, int], int] = {}
    for row, (frame_id, _) in enumerate(page_hashes):
        for column, (reference_id, _) in enumerate(reference_hashes):
            distance = int(matrix[row, column])
            if distance <= 40:
                key = (frame_id, reference_id)
                expected[key] = min(expected.get(key, distance), distance)

    assert expected
    assert scan_candidates(reference_hashes, page_hashes, 40) == expected
    assert scan_candidates([], page_hashes, 40) == {}
