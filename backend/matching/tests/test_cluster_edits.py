from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client

from matching.models import Cluster, ClusterMember, PairLabel
from matching.services import build_clusters, label_pair, merge_clusters, move_member
from matching.tests.test_services import MakePage, add_frames


@pytest.mark.parametrize("status", ["confirmed", "proposed", "rejected"])
def test_merge_preserves_status(make_page: MakePage, status: str) -> None:
    target, source = Cluster.objects.create(), Cluster.objects.create()
    member = ClusterMember.objects.create(
        cluster=source, page=make_page("a"), status=status, evidence="pdq", borderline=True
    )
    merge_clusters(target, [source])
    member.refresh_from_db()
    assert member.cluster == target
    assert member.status == status
    assert member.borderline
    assert not Cluster.objects.filter(pk=source.pk).exists()


@pytest.mark.parametrize("status", ["proposed", "rejected"])
def test_duplicate_precedence(make_page: MakePage, status: str) -> None:
    target, source = Cluster.objects.create(), Cluster.objects.create()
    page = make_page("a")
    ClusterMember.objects.create(cluster=target, page=page, status="rejected", evidence="manual")
    ClusterMember.objects.create(cluster=source, page=page, status=status, evidence="pdq")
    merge_clusters(target, [source])
    assert target.members.get().status == status


@pytest.mark.parametrize("operation", ["merge", "move"])
@pytest.mark.parametrize("conflict", ["duplicate", "label", "derived"])
def test_conflicts_atomic(make_page: MakePage, operation: str, conflict: str) -> None:
    target, source = Cluster.objects.create(), Cluster.objects.create()
    a, b = make_page("a"), make_page("b")
    member = ClusterMember.objects.create(
        cluster=source, page=a, status="confirmed", evidence="manual"
    )
    ClusterMember.objects.create(
        cluster=target,
        page=a if conflict == "duplicate" else b,
        status="rejected" if conflict == "duplicate" else "confirmed",
        evidence="manual",
    )
    if conflict == "label":
        label_pair(a, b, PairLabel.Label.DIFFERENT)
    if conflict == "derived":
        ClusterMember.objects.create(cluster=source, page=b, status="rejected", evidence="pdq")
    before = list(ClusterMember.objects.order_by("pk").values())
    clusters = list(Cluster.objects.values())
    with pytest.raises(ValueError):
        if operation == "merge":
            merge_clusters(target, [source])
        else:
            move_member(member, target)
    assert list(ClusterMember.objects.order_by("pk").values()) == before
    assert list(Cluster.objects.values()) == clusters


def test_move_deletes_empty_source(make_page: MakePage) -> None:
    source, target = Cluster.objects.create(), Cluster.objects.create()
    member = ClusterMember.objects.create(cluster=source, page=make_page("a"), evidence="pdq")
    assert move_member(member, target).cluster == target
    assert not Cluster.objects.filter(pk=source.pk).exists()


def test_review_band_and_stale(make_page: MakePage) -> None:
    a, b, c = [make_page(name) for name in "abc"]
    for page, value in ((a, 0), (b, 1), (c, (1 << 40) - 1)):
        add_frames(page, [value.to_bytes(32, "big")])
    out = StringIO()
    call_command(
        "build_clusters", "--max-distance", "24", "--review-max-distance", "60", stdout=out
    )
    assert ClusterMember.objects.get(page=c).borderline
    assert not ClusterMember.objects.filter(page__in=(a, b), borderline=True).exists()
    build_clusters(max_distance=24)
    assert not ClusterMember.objects.filter(page=c).exists()
    build_clusters(max_distance=24, review_max_distance=60)
    ClusterMember.objects.filter(page=c).update(status="confirmed")
    before = ClusterMember.objects.filter(page=c).values().get()
    build_clusters(max_distance=24)
    assert ClusterMember.objects.filter(page=c).values().get() == before


@pytest.mark.parametrize("conflict", [False, True])
def test_admin_move(make_page: MakePage, admin_client: Client, conflict: bool) -> None:
    source, target = Cluster.objects.create(), Cluster.objects.create()
    page = make_page("a")
    member = ClusterMember.objects.create(
        cluster=source, page=page, evidence="pdq", status="confirmed" if conflict else "proposed"
    )
    ClusterMember.objects.create(cluster=target, page=page, evidence="manual", status="rejected")
    response = admin_client.post(
        f"/admin/matching/clustermember/{member.pk}/change/",
        {"cluster": target.pk, "status": member.status, "evidence": "pdq", "min_distance": ""},
    )
    assert response.status_code == (200 if conflict else 302)
    member.refresh_from_db()
    assert member.cluster_id == (source.pk if conflict else target.pk)
    assert Cluster.objects.filter(pk=source.pk).exists() == conflict


@pytest.mark.parametrize("conflict", [False, True])
def test_admin_merge(make_page: MakePage, admin_client: Client, conflict: bool) -> None:
    target, source = Cluster.objects.create(), Cluster.objects.create()
    a, b = make_page("a"), make_page("b")
    for cluster, page in ((target, a), (source, b)):
        ClusterMember.objects.create(
            cluster=cluster, page=page, evidence="manual", status="confirmed"
        )
    if conflict:
        label_pair(a, b, PairLabel.Label.DIFFERENT)
    response = admin_client.post(
        "/admin/matching/cluster/",
        {"action": "merge_selected_clusters", "_selected_action": [source.pk, target.pk]},
        follow=True,
    )
    assert response.status_code == 200
    assert Cluster.objects.filter(pk=source.pk).exists() == conflict
    assert target.members.count() == (1 if conflict else 2)


def test_sha_is_not_borderline(make_page: MakePage) -> None:
    from index.models import Media

    for name, value in (("a", 0), ("b", (1 << 40) - 1)):
        page = make_page(name)
        add_frames(page, [value.to_bytes(32, "big")])
        Media.objects.filter(page=page).update(sha256="a" * 64)
    build_clusters(max_distance=24, review_max_distance=60)
    assert ClusterMember.objects.count() == 2
    assert not ClusterMember.objects.filter(borderline=True).exists()


def test_invalid_review_distance(make_page: MakePage) -> None:
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="review-max-distance"):
        call_command("build_clusters", "--max-distance", "24", "--review-max-distance", "23")
