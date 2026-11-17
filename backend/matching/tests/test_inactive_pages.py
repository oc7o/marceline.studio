"""Inactive pages (issue #71) are kept but never queried by matching, clustering, duplicates,
common frames, leak suggestions or the media queue; reactivating one brings it back."""

import hashlib
from datetime import timedelta

import pytest
from django.utils import timezone

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from matching import media as queue
from matching.common_frames import load_candidate_frames
from matching.leaks import active_cluster_pages
from matching.models import (
    Cluster,
    ClusterMember,
    Reference,
    ReferenceFrameHash,
    ReferenceLeak,
    ReferenceMatch,
)
from matching.references import match_references
from matching.services import find_candidates, load_page_hashes, mirror_groups
from matching.tests.hashes import flip_bits, random_hashes

pytestmark = pytest.mark.django_db

SCENE_A, SCENE_B, SCENE_C = random_hashes(3, seed=71)
UNTARGETED = Page.InactiveReason.UNTARGETED


def deactivate(*pages: Page) -> None:
    Page.objects.filter(pk__in=[page.pk for page in pages]).update(
        active=False, inactive_reason=UNTARGETED, active_changed_at=timezone.now()
    )


def reactivate(*pages: Page) -> None:
    Page.objects.filter(pk__in=[page.pk for page in pages]).update(
        active=True, inactive_reason="", active_changed_at=timezone.now()
    )


def page_with_frames(site: Site, slug: str, hashes: list[bytes], *, kind: str = "frame") -> Page:
    page = Page.objects.create(
        site=site, url=f"https://{site.domain}/{slug}", title=slug, safety_score=1.0
    )
    for position, value in enumerate(hashes, start=1):
        media = Media.objects.create(
            page=page,
            kind=kind,
            url=f"https://cdn.test/{page.pk}/thumb_{position}.jpg",
        )
        FrameHash.objects.create(media=media, pdq=value, quality=100)
    return page


def make_reference(title: str) -> Reference:
    creator = Creator.objects.create(name=f"Creator {title}")
    reference = Reference.objects.create(
        creator=creator,
        title=title,
        sha256=hashlib.sha256(title.encode()).hexdigest(),
        frame_fps=1,
        duration_s=300,
    )
    for second, value in enumerate((SCENE_A, SCENE_B, SCENE_C), start=100):
        ReferenceFrameHash.objects.create(
            reference=reference, frame_index=second, timestamp_s=second, pdq=value, quality=100
        )
    return reference


def test_inactive_page_is_not_matched_until_reactivated(site: Site) -> None:
    reference = make_reference("Original")
    leak = page_with_frames(site, "leak", [flip_bits(SCENE_A, 4), flip_bits(SCENE_B, 5)])
    deactivate(leak)

    match_references([reference])
    assert not ReferenceMatch.objects.exists()

    # A reactivated page's frames are old; the incremental scan still has to see them.
    reactivate(leak)
    lines = match_references([reference])

    assert lines[-1].startswith("Scan: incremental")
    assert list(ReferenceMatch.objects.values_list("page_id", flat=True)) == [leak.pk]


def test_deactivation_drops_proposals_but_keeps_human_decisions(site: Site) -> None:
    reference = make_reference("Original")
    proposed = page_with_frames(site, "proposed", [flip_bits(SCENE_A, 3), flip_bits(SCENE_B, 3)])
    decided = page_with_frames(site, "decided", [flip_bits(SCENE_A, 2), flip_bits(SCENE_C, 2)])
    match_references([reference], full=True)
    ReferenceMatch.objects.filter(page=decided).update(status="confirmed")

    deactivate(proposed, decided)
    match_references([reference], full=True)

    assert list(ReferenceMatch.objects.values_list("page_id", "status")) == [
        (decided.pk, "confirmed")
    ]


def test_duplicates_mirrors_and_common_frames_skip_inactive_pages(site: Site) -> None:
    first = page_with_frames(site, "first", [SCENE_A], kind="poster")
    second = page_with_frames(site, "second", [flip_bits(SCENE_A, 3)], kind="poster")
    # One mirror poster key served from two hosts.
    for page, host in ((first, "a.cdn.test"), (second, "b.cdn.test")):
        Media.objects.create(
            page=page, kind="poster", url=f"https://{host}/imgs/1/2/0123456789abcde_main.jpg"
        )

    assert set(load_page_hashes()) == {first.pk, second.pk}
    assert len(find_candidates()) == 1
    assert mirror_groups() == [{first.pk, second.pk}]

    deactivate(second)

    assert set(load_page_hashes()) == {first.pk}
    assert set(load_page_hashes([second.pk])) == {second.pk}  # human labels still load
    assert find_candidates() == []
    assert mirror_groups() == []
    assert {frame.page_id for frame in load_candidate_frames()} == {first.pk}


def test_media_queue_skips_inactive_pages(site: Site) -> None:
    page = Page.objects.create(site=site, url="https://example.test/p", safety_score=1.0)
    Media.objects.create(
        page=page,
        kind="poster",
        url="https://cdn.test/p.jpg",
        eligible_at=timezone.now() - timedelta(minutes=1),
    )
    deactivate(page)
    assert queue.claim_media() is None

    reactivate(page)
    claimed = queue.claim_media()
    assert claimed is not None and claimed.page_id == page.pk


def test_cluster_propagation_skips_inactive_members(site: Site) -> None:
    reference = make_reference("Original")
    strong = page_with_frames(site, "strong", [flip_bits(SCENE_A, 3), flip_bits(SCENE_B, 3)])
    sibling = page_with_frames(site, "sibling", [SCENE_C], kind="poster")
    dormant = page_with_frames(site, "dormant", [], kind="poster")
    cluster = Cluster.objects.create()
    for page in (strong, sibling, dormant):
        ClusterMember.objects.create(cluster=cluster, page=page, evidence="manual")
    deactivate(dormant)

    assert active_cluster_pages() == [{strong.pk, sibling.pk}]
    match_references([reference])
    assert set(
        ReferenceLeak.objects.filter(source="cluster").values_list("page_id", flat=True)
    ) == {sibling.pk}
