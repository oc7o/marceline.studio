import hashlib

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from matching.admin import ReferenceAdmin, ReferenceMatchAdmin, format_moments
from matching.creators import set_creators
from matching.models import Cluster, ClusterMember, Reference, ReferenceFrameHash, ReferenceMatch
from matching.references import (
    COMMON_PAGE_GROUPS,
    SharedCandidate,
    find_shared_frames,
    mark_shared_frames,
    match_references,
    reference_report,
)
from matching.tests.hashes import flip_bits, random_hashes

pytestmark = pytest.mark.django_db

INTRO, OUTRO, SCENE_A, SCENE_B, SCENE_C = random_hashes(5, seed=10)


@pytest.fixture
def creator() -> Creator:
    return Creator.objects.create(name="Synthetic Creator")


def make_reference(creator: Creator, title: str, frames: dict[int, bytes]) -> Reference:
    """Reference at 1 fps with two orientations per frame (the second an inverted hash)."""
    reference = Reference.objects.create(
        creator=creator, title=title, sha256=hashlib.sha256(title.encode()).hexdigest(), frame_fps=1
    )
    for second, value in frames.items():
        for orientation, variant in enumerate((value, bytes(b ^ 0xFF for b in value))):
            ReferenceFrameHash.objects.create(
                reference=reference,
                frame_index=second,
                timestamp_s=second,
                pdq=variant,
                quality=100,
                dihedral=orientation,
            )
    return reference


def make_page(site: Site, slug: str, values: list[bytes]) -> Page:
    page = Page.objects.create(site=site, title=slug, url=f"https://{site.domain}/{slug}")
    media = Media.objects.create(page=page, kind="frame", url=f"https://{site.domain}/{slug}.png")
    for index, value in enumerate(values):
        FrameHash.objects.create(media=media, frame_index=index, pdq=value, quality=100)
    return page


def shared_rows() -> set[tuple[int, int, int]]:
    return set(
        ReferenceFrameHash.objects.filter(shared=True).values_list(
            "reference_id", "frame_index", "dihedral"
        )
    )


def test_intro_shared_between_references(creator: Creator) -> None:
    first = make_reference(creator, "First", {0: INTRO, 1: SCENE_A})
    second = make_reference(creator, "Second", {0: flip_bits(INTRO, 20), 1: SCENE_B})
    assert mark_shared_frames() == 2
    # All orientations of a shared position are marked; unique frames are not.
    assert shared_rows() == {(ref.pk, 0, d) for ref in (first, second) for d in (0, 1)}


def test_only_original_orientations_compared_across_references() -> None:
    # References 1 and 2 belong to the same creator: a common intro is still shared.
    one = frozenset({9})
    first, second = "a" * 64, "b" * 64
    frames = [
        SharedCandidate(1, first, one, 0, 0, SCENE_A),
        SharedCandidate(1, first, one, 0, 1, INTRO),
    ]
    frames.append(SharedCandidate(2, second, one, 0, 0, INTRO))
    assert find_shared_frames(frames, [], {}, {}) == set()
    frames.append(SharedCandidate(1, first, one, 3, 0, flip_bits(INTRO, 31)))
    assert find_shared_frames(frames, [], {}, {}) == {(1, 3), (2, 0)}


def test_same_file_on_two_pipelines_is_one_original() -> None:
    """A v0 and a v1 fingerprint of one file must not declare each other's frames common footage.

    During re-fingerprinting (issue #70) both rows exist. Their grids are half a second apart, so
    on slow-moving material the hashes land within the threshold — which, without this rule, would
    mark every frame of both originals as shared and destroy the evidence of both.
    """
    one = frozenset({9})
    digest = "a" * 64
    frames = [
        SharedCandidate(1, digest, one, 0, 0, INTRO),
        SharedCandidate(2, digest, one, 0, 0, flip_bits(INTRO, 20)),
    ]
    assert find_shared_frames(frames, [], {}, {}) == set()
    # A genuinely different original sharing that intro is still shared.
    frames.append(SharedCandidate(3, "c" * 64, one, 0, 0, flip_bits(INTRO, 8)))
    assert find_shared_frames(frames, [], {}, {}) == {(1, 0), (2, 0), (3, 0)}


def test_own_creator_pages_do_not_make_a_frame_shared(creator: Creator, site: Site) -> None:
    reference = make_reference(creator, "Original", {0: INTRO, 1012: SCENE_A})
    for i in range(COMMON_PAGE_GROUPS + 2):
        make_page(site, f"leak-{i}", [flip_bits(SCENE_A, 4, seed=i)]).creators.add(creator)
    assert mark_shared_frames() == 0
    output = match_references(Reference.objects.filter(pk=reference.pk))
    assert f"{COMMON_PAGE_GROUPS + 2} pages matched" in output[0]
    for match in ReferenceMatch.objects.all():
        assert (match.matched_seconds, match.shared_seconds) == ([1012], [])


def test_co_performer_pages_do_not_make_a_frame_shared(creator: Creator, site: Site) -> None:
    """Pages found under a co-performer's name are likely leaks, not unrelated footage (#64)."""
    partner = Creator.objects.create(name="Carla Bremer")
    reference = make_reference(creator, "Original", {0: INTRO, 1012: SCENE_A})
    set_creators(reference, [partner])
    for i in range(COMMON_PAGE_GROUPS + 2):
        make_page(site, f"leak-{i}", [flip_bits(SCENE_A, 4, seed=i)]).creators.add(partner)
    assert mark_shared_frames() == 0
    # Without the co-performer link the very same pages mark the scene as shared footage.
    reference.creators.set([creator])
    assert mark_shared_frames() == 1
    assert shared_rows() == {(reference.pk, 1012, 0), (reference.pk, 1012, 1)}


def test_other_creators_pages_make_a_frame_shared(creator: Creator, site: Site) -> None:
    reference = make_reference(creator, "Original", {0: INTRO, 1012: SCENE_A})
    other = Creator.objects.create(name="Other Creator")
    for i in range(COMMON_PAGE_GROUPS):
        page = make_page(site, f"other-{i}", [flip_bits(INTRO, 4, seed=i)])
        page.creators.add(other)
    # A page linked to both creators is skipped for this reference.
    make_page(site, "both", [INTRO]).creators.add(creator, other)
    assert mark_shared_frames() == 1
    assert shared_rows() == {(reference.pk, 0, 0), (reference.pk, 0, 1)}


def test_page_group_rule(creator: Creator, site: Site) -> None:
    reference = make_reference(creator, "Original", {0: INTRO, 5: SCENE_A})
    pages = [
        make_page(site, f"page-{i}", [flip_bits(INTRO, 3, seed=i)])
        for i in range(COMMON_PAGE_GROUPS - 1)
    ]
    # Two frames of one page and two pages of one active cluster each count as one group.
    clustered = make_page(site, "clustered", [INTRO, flip_bits(INTRO, 5)])
    cluster = Cluster.objects.create()
    for page in (pages[0], clustered):
        ClusterMember.objects.create(cluster=cluster, page=page, evidence="manual")
    make_page(site, "scene", [SCENE_A])
    assert mark_shared_frames() == 0
    make_page(site, "unrelated", [INTRO])
    assert mark_shared_frames() == 1
    assert shared_rows() == {(reference.pk, 0, 0), (reference.pk, 0, 1)}


def test_idempotent_recomputation(creator: Creator) -> None:
    first = make_reference(creator, "First", {0: INTRO, 1: SCENE_A})
    second = make_reference(creator, "Second", {0: INTRO})
    assert mark_shared_frames() == 2
    before = shared_rows()
    assert mark_shared_frames() == 2
    assert shared_rows() == before
    second.delete()
    assert mark_shared_frames() == 0
    assert not shared_rows()
    assert first.frames.count() == 4


def test_match_strength_seconds_and_human_status(creator: Creator, site: Site) -> None:
    original = make_reference(
        creator, "Original", {0: INTRO, 1012: SCENE_A, 1013: SCENE_B, 2980: OUTRO}
    )
    make_reference(creator, "Same distributor", {0: INTRO, 7: SCENE_C})
    intro = make_page(site, "intro", [INTRO])
    leak = make_page(site, "leak", [flip_bits(SCENE_A, 18), flip_bits(SCENE_B, 26)])
    mixed = make_page(site, "mixed", [INTRO, flip_bits(OUTRO, 16)])
    output = match_references(Reference.objects.filter(pk=original.pk))
    assert "3 pages matched (1 strong, 2 weak)" in output[0]

    intro_match = ReferenceMatch.objects.get(reference=original, page=intro)
    assert intro_match.strength == "weak"
    assert intro_match.evidence_frames == 0 and intro_match.matched_frames == 1
    assert intro_match.matched_seconds == [] and intro_match.shared_seconds == [0]
    assert intro_match.best_distance == 0

    leak_match = ReferenceMatch.objects.get(reference=original, page=leak)
    assert leak_match.strength == "strong"
    assert leak_match.evidence_frames == 2
    assert leak_match.matched_seconds == [1012, 1013] and leak_match.shared_seconds == []
    assert (leak_match.best_distance, leak_match.best_reference_timestamp_s) == (18, 1012)

    mixed_match = ReferenceMatch.objects.get(reference=original, page=mixed)
    assert mixed_match.strength == "weak"
    assert mixed_match.evidence_frames == 1 and mixed_match.matched_frames == 2
    assert mixed_match.matched_seconds == [2980] and mixed_match.shared_seconds == [0]
    # Best evidence prefers the non-shared frame over a closer shared one.
    assert (mixed_match.best_distance, mixed_match.best_reference_timestamp_s) == (16, 2980)

    leak_match.status = "confirmed"
    leak_match.save()
    intro_match.status = "rejected"
    intro_match.save()
    report = reference_report([original])
    assert "  strong: 1 confirmed, 0 rejected (false matches), 0 proposed (unreviewed)" in report
    assert "  weak: 0 confirmed, 1 rejected (false matches), 1 proposed (unreviewed)" in report

    FrameHash.objects.filter(media__page=leak, frame_index=1).delete()
    FrameHash.objects.filter(media__page=mixed).delete()
    match_references(Reference.objects.all())
    leak_match.refresh_from_db()
    intro_match.refresh_from_db()
    assert (leak_match.status, leak_match.strength, leak_match.evidence_frames) == (
        "confirmed",
        "weak",
        1,
    )
    assert intro_match.status == "rejected"
    assert not ReferenceMatch.objects.filter(page=mixed).exists()


def test_admin_moments_and_shared_count(creator: Creator, site: Site) -> None:
    assert format_moments([1012, 1013], [2980]) == "16:52, 16:53 · shared: 49:40"
    assert format_moments([], [0, 5]) == "shared: 00:00, 00:05"
    assert format_moments([3700], []) == "61:40"
    assert format_moments([], []) == "—"
    assert format_moments(list(range(14)), []).endswith("00:11 (+2)")

    original = make_reference(creator, "Original", {0: INTRO, 1012: SCENE_A})
    make_reference(creator, "Other", {0: INTRO})
    make_page(site, "page", [INTRO, SCENE_A])
    match_references(Reference.objects.filter(pk=original.pk))
    match = ReferenceMatch.objects.get()
    match_admin = ReferenceMatchAdmin(ReferenceMatch, AdminSite())
    assert match_admin.moments(match) == "16:52 · shared: 00:00"
    admin = ReferenceAdmin(Reference, AdminSite())
    row = admin.get_queryset(RequestFactory().get("/admin/")).get(pk=original.pk)
    assert admin.shared_frames_count(row) == 1
    assert admin.frames_count(row) == 2
