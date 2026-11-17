from collections.abc import Callable
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client

from catalog.models import Creator
from index.models import FrameHash, Media, Page
from matching.creators import set_creators
from matching.hamming import HashIndex
from matching.leak_scoring import score_page
from matching.leaks import suggest_leaks
from matching.m2 import m2_report, miss_reason
from matching.models import Reference, ReferenceFrameHash, ReferenceLeak, ReferenceMatch
from matching.references import select_references


@pytest.fixture
def reference(db: None) -> Reference:
    creator = Creator.objects.create(name="Synthetic Artist", aliases=["Test Alias"])
    return Reference.objects.create(
        creator=creator,
        title="Synthetic Artist mountain sunrise",
        duration_s=1000,
        sha256="a" * 64,
        frame_fps=1,
    )


def test_scoring() -> None:
    def score(title: str, duration: float | None, linked: bool = False) -> float:
        return score_page(
            "Artist mountain sunrise",
            1000,
            title,
            {"duration_s": duration},
            ["Artist"],
            linked,
            None,
        ).score

    assert score("other", 1001, True) > score("mountain sunrise", None)
    assert score("other", 50, True) == 0
    evidence = score_page(
        "Synthetic Artist sunrise",
        1000,
        "Test Alias mountain",
        {},
        ["Synthetic Artist", "Test Alias"],
        False,
        None,
    )
    assert evidence.signals["title_similarity"] == 0


@pytest.mark.django_db
def test_suggestions_preserve_refresh_remove_and_dry_run(
    reference: Reference, make_page: Callable[[str], Page]
) -> None:
    pages = [make_page(str(i)) for i in range(5)]
    for page in pages:
        page.creators.add(reference.creator)
        page.raw_extraction = {"duration_s": 1000}
        page.save()
    confirmed = ReferenceLeak.objects.create(reference=reference, page=pages[0], status="confirmed")
    rejected = ReferenceLeak.objects.create(reference=reference, page=pages[1], status="rejected")
    stale_page = make_page("unrelated")
    stale = ReferenceLeak.objects.create(reference=reference, page=stale_page)
    match(reference, pages[2], "confirmed")
    output = StringIO()
    call_command("suggest_leaks", dry_run=True, stdout=output)
    assert ReferenceLeak.objects.count() == 3
    assert "dry run" in output.getvalue()
    suggest_leaks(select_references())
    assert not ReferenceLeak.objects.filter(pk=stale.pk).exists()
    assert not ReferenceLeak.objects.filter(page=pages[2]).exists()
    for row, status in [(confirmed, "confirmed"), (rejected, "rejected")]:
        row.refresh_from_db()
        assert row.status == status
        assert row.score == 0
    proposal = ReferenceLeak.objects.get(page=pages[3])
    pages[3].raw_extraction = {"duration_s": 1000}
    pages[3].save()
    suggest_leaks(select_references())
    proposal.refresh_from_db()
    assert proposal.score == pytest.approx(0.5)
    pages[3].creators.clear()
    suggest_leaks(select_references())
    assert not ReferenceLeak.objects.filter(pk=proposal.pk).exists()


@pytest.mark.django_db
def test_co_performer_link_scores_duration_candidates(
    reference: Reference, make_page: Callable[[str], Page]
) -> None:
    """Duration-only candidates need a creator link — any performer's link counts (issue #64)."""
    partner = Creator.objects.create(name="Carla Bremer")
    set_creators(reference, [partner])
    page = make_page("co-performer")
    page.creators.add(partner)
    page.raw_extraction = {"duration_s": 1000}
    page.save()
    suggest_leaks(select_references())
    leak = ReferenceLeak.objects.get(page=page)
    assert leak.score == pytest.approx(0.5)
    assert leak.signals["creator_linked"] is True
    # Losing every link to the original removes the proposal again.
    page.creators.clear()
    suggest_leaks(select_references())
    assert not ReferenceLeak.objects.filter(page=page).exists()


def match(
    reference: Reference, page: Page, status: str, strength: str = "strong"
) -> ReferenceMatch:
    return ReferenceMatch.objects.create(
        reference=reference,
        page=page,
        status=status,
        strength=strength,
        best_distance=10,
        matched_frames=2,
        page_frames=2,
        best_reference_timestamp_s=20,
    )


@pytest.mark.django_db
def test_report_math(reference: Reference, make_page: Callable[[str], Page]) -> None:
    pages = [make_page(str(i)) for i in range(6)]
    for page in pages[:3]:
        ReferenceLeak.objects.create(reference=reference, page=page, status="confirmed")
    match(reference, pages[0], "confirmed")  # Union deduplicates this pair.
    match(reference, pages[1], "proposed", "weak")
    match(reference, pages[3], "confirmed")  # Ground truth without a Leak row.
    match(reference, pages[4], "proposed")
    ReferenceLeak.objects.create(reference=reference, page=pages[4], status="rejected")
    match(reference, pages[5], "rejected")
    report = "\n".join(m2_report(select_references()))
    assert "confirmed leaks=4; found strong=2, weak=1" in report
    assert "recall strong=50.0%, strong+weak=75.0%" in report
    assert "false matches=2/5 reviewed (40.0%)" in report
    assert "M2 exit: FAIL" in report
    assert "too few to judge" in report
    assert f"Miss page {pages[2].pk}: no FrameHash rows; media: none" in report


@pytest.mark.django_db
@pytest.mark.parametrize("case", ["linked", "rejected member", "weak seed", "rejected seed"])
def test_report_linked_via_cluster(
    reference: Reference, make_page: Callable[[str], Page], case: str
) -> None:
    from matching.models import Cluster, ClusterMember

    seed, leak = make_page("seed"), make_page("leak")
    cluster = Cluster.objects.create()
    ClusterMember.objects.create(cluster=cluster, page=seed, evidence="manual")
    ClusterMember.objects.create(
        cluster=cluster,
        page=leak,
        evidence="manual",
        status="rejected" if case == "rejected member" else "proposed",
    )
    if case == "rejected seed":
        match(reference, seed, "rejected")
    else:
        match(reference, seed, "proposed", "weak" if case == "weak seed" else "strong")
        ReferenceLeak.objects.create(reference=reference, page=seed, status="confirmed")
    ReferenceLeak.objects.create(reference=reference, page=leak, status="confirmed")
    report = "\n".join(m2_report(select_references()))
    if case == "linked":
        assert "confirmed leaks=2; found strong=1, weak=0, linked=1" in report
        assert "strong+weak=50.0%, strong+weak+linked=100.0%" in report
        assert f"Linked page {leak.pk}: active member of cluster {cluster.pk}" in report
        assert "Miss page" not in report
        assert "M2 exit: PASS" in report
        assert "strong+weak+linked recall ≥70% (now 100.0%)" in report
    else:
        assert "linked=0" in report
        assert f"Miss page {leak.pk}: no FrameHash rows" in report
        assert "M2 exit: FAIL" in report


@pytest.mark.django_db
def test_miss_reasons(reference: Reference, make_page: Callable[[str], Page]) -> None:
    page = make_page("miss")
    pending = Media.objects.create(page=page, kind="poster", url="https://example.test/pending")
    expired = Media.objects.create(
        page=page,
        kind="poster",
        url="https://example.test/expired",
        status="skipped",
        last_error="URL expired",
    )
    index = HashIndex.from_pairs([(1, bytes(32))])
    assert "media: pending, skipped expired" in miss_reason(page, index, 31)
    frame = FrameHash.objects.create(media=pending, pdq=b"\xff" * 32, quality=100)
    assert "only 1 frames" in miss_reason(page, index, 31)
    assert "distance to non-shared reference frame is 256" in miss_reason(page, index, 31)
    ReferenceFrameHash.objects.create(
        reference=reference, frame_index=0, timestamp_s=0, pdq=bytes(32), quality=100
    )
    ReferenceLeak.objects.create(reference=reference, page=page, status="confirmed")
    assert "is 256" in "\n".join(m2_report(select_references()))
    frame.quality = 0
    frame.save()
    assert "all frames below quality" in miss_reason(page, index, 31)
    page.availability = "removed"
    assert miss_reason(page, index, 31).startswith("page removed;")
    expired.delete()


@pytest.mark.django_db
def test_admin_text_only(
    reference: Reference, make_page: Callable[[str], Page], admin_client: Client
) -> None:
    page = make_page('<img src="https://example.test/image"><iframe>')
    ReferenceLeak.objects.create(reference=reference, page=page)
    response = admin_client.get("/admin/matching/referenceleak/")
    assert response.status_code == 200
    html = response.content.decode()
    assert "<img" not in html
    assert "<iframe" not in html
    assert "&lt;img" in html


@pytest.mark.django_db
def test_title_candidates_and_limit(reference: Reference, make_page: Callable[[str], Page]) -> None:
    page = make_page("mountain sunrise")
    suggest_leaks(select_references(), limit_per_reference=1)
    assert not ReferenceLeak.objects.exists()
    match(reference, page, "proposed", "weak")
    suggest_leaks(select_references(), limit_per_reference=1)
    assert ReferenceLeak.objects.get().page == page
    with pytest.raises(ValueError):
        suggest_leaks(select_references(), limit_per_reference=0)


@pytest.mark.parametrize("diff,eligible", [(1.5, True), (-1.5, True), (1.51, False), (-2, False)])
def test_duration_boundary(diff: float, eligible: bool) -> None:
    result = score_page("sunrise", 1000, "sunrise", {"duration_s": 1000 + diff}, [], True, None)
    assert (result.score >= 0.5) is eligible
    assert result.signals["title_similarity"] == 1


@pytest.mark.django_db
def test_cluster_propagation(reference: Reference, make_page: Callable[[str], Page]) -> None:
    from matching.leaks import propagate_cluster_leaks, rank_candidates
    from matching.models import Cluster, ClusterMember

    seed, target, rejected = [make_page(name) for name in ("seed", "target", "rejected")]
    cluster = Cluster.objects.create()
    for page, status in [(seed, "confirmed"), (target, "proposed"), (rejected, "rejected")]:
        ClusterMember.objects.create(cluster=cluster, page=page, status=status, evidence="manual")
    match(reference, seed, "proposed")
    propagate_cluster_leaks(reference, {seed.pk})
    proposal = ReferenceLeak.objects.get(page=target)
    assert proposal.source == "cluster"
    assert proposal.signals["matched_page_id"] == str(seed.pk)
    assert {s.page.pk for s in rank_candidates(reference)} == {seed.pk, target.pk}
    assert not ReferenceLeak.objects.filter(page=rejected).exists()
    propagate_cluster_leaks(reference, set())
    assert not ReferenceLeak.objects.exists()
    propagate_cluster_leaks(reference, {seed.pk})
    ReferenceLeak.objects.filter(page=target).update(status="confirmed", note="human")
    propagate_cluster_leaks(reference, set())
    assert ReferenceLeak.objects.get(page=target).note == "human"
    ReferenceLeak.objects.filter(page=target).update(status="rejected")
    propagate_cluster_leaks(reference, {seed.pk})
    assert ReferenceLeak.objects.get(page=target).status == "rejected"


@pytest.mark.django_db
def test_matching_refreshes_propagation(
    reference: Reference, make_page: Callable[[str], Page]
) -> None:
    from matching.models import Cluster, ClusterMember
    from matching.references import match_references

    seed, target = make_page("seed"), make_page("target")
    seed.creators.add(reference.creator)
    cluster = Cluster.objects.create()
    for page in (seed, target):
        ClusterMember.objects.create(cluster=cluster, page=page, evidence="manual")
    media = Media.objects.create(page=seed, kind="sprite", url="https://cdn.test/sprite")
    for i, pdq in enumerate([bytes(32), b"\xff" * 32]):
        FrameHash.objects.create(media=media, frame_index=i, pdq=pdq, quality=100)
        ReferenceFrameHash.objects.create(
            reference=reference, frame_index=i, timestamp_s=i, pdq=pdq, quality=100
        )
    match_references([reference])
    assert ReferenceLeak.objects.get(page=target).source == "cluster"
    FrameHash.objects.all().delete()
    match_references([reference])
    assert not ReferenceLeak.objects.exists()


@pytest.mark.django_db
def test_pending_mirror_suggestion(reference: Reference, make_page: Callable[[str], Page]) -> None:
    from matching.leaks import rank_candidates

    seed, target = make_page("seed"), make_page("unrelated title")
    match(reference, seed, "proposed")
    for page, host in [(seed, "one.test"), (target, "two.test")]:
        Media.objects.create(
            page=page,
            kind="poster",
            url=f"https://{host}/imgs/34/99/1a2b3c4d5e6f708_main.jpg",
        )
    ranked = rank_candidates(reference)
    assert [item.page.pk for item in ranked] == [seed.pk, target.pk]
    assert ranked[1].evidence.signals["related_kind"] == "mirror"


@pytest.mark.django_db
def test_evidence_backed_uncapped_duration_only_capped(
    reference: Reference, make_page: Callable[[str], Page]
) -> None:
    from matching.models import Cluster, ClusterMember

    matched = []
    for i in range(6):
        page = make_page(f"unrelated-{i}")
        page.raw_extraction = {"duration_s": 10}
        page.save()
        match(reference, page, "proposed", "weak")
        matched.append(page)
    cluster = Cluster.objects.create()
    ClusterMember.objects.create(cluster=cluster, page=matched[0], evidence="manual")
    member = make_page("cluster-member")
    ClusterMember.objects.create(cluster=cluster, page=member, evidence="manual")
    ReferenceMatch.objects.filter(page=matched[0]).update(strength="strong")
    duration_only = []
    for i in range(7):
        page = make_page(f"same-length-{i}")
        page.creators.add(reference.creator)
        page.raw_extraction = {"duration_s": 1000}
        page.save()
        duration_only.append(page.pk)
    suggest_leaks(select_references())
    pages = set(ReferenceLeak.objects.values_list("page_id", flat=True))
    # 6 matches and 1 cluster relation, all uncapped; duration-only limited to the default 5.
    assert {page.pk for page in matched} | {member.pk} <= pages
    assert len(pages & set(duration_only)) == 5
    assert len(pages) == 12
    suggest_leaks(select_references(), limit_per_reference=1)
    pages = set(ReferenceLeak.objects.values_list("page_id", flat=True))
    assert len(pages) == 8 and len(pages & set(duration_only)) == 1
