from io import StringIO

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction

from catalog.models import Site
from index.models import Media
from matching.clustering import Edge
from matching.models import Cluster, ClusterMember, PairLabel
from matching.services import build_clusters, evidence_edges, label_pair, labeled_pairs
from matching.tests.test_services import MakePage, add_frames


def test_evidence(make_page: MakePage) -> None:
    a, b = make_page("a"), make_page("b")
    for p in (a, b):
        add_frames(p, [bytes(32)], quality=60)
        Media.objects.filter(page=p).update(sha256="a" * 64)
        p.raw_extraction = {"external_links": ["https://files.test/video", "https://t.me/promo"]}
        p.save()
    assert {e.evidence for e in evidence_edges()} == {"pdq", "sha256", "external_link"}
    assert {e.evidence for e in evidence_edges(min_quality=61)} == {"sha256"}
    assert {e.evidence for e in evidence_edges(min_score=1.1)} == {"sha256"}
    for i in range(4):
        p = make_page(str(i))
        p.raw_extraction = {"external_links": ["https://files.test/video"]}
        p.save()
    assert {e.evidence for e in evidence_edges()} == {"pdq", "sha256"}


def test_labels_transitive_idempotent_and_dry_run(make_page: MakePage) -> None:
    a, b, c = [make_page(p) for p in "abc"]
    label_pair(a, b, PairLabel.Label.SAME)
    label_pair(b, c, PairLabel.Label.SAME)
    out = StringIO()
    call_command("build_clusters", "--dry-run", stdout=out)
    assert "1 clusters created / 0 members proposed" in out.getvalue()
    assert not Cluster.objects.exists()
    assert build_clusters() == (1, 0)
    assert build_clusters() == (0, 0)
    assert ClusterMember.objects.filter(status="confirmed").count() == 3
    assert len(labeled_pairs()) == 3
    cluster = Cluster.objects.get()
    a.delete()
    assert Cluster.objects.filter(pk=cluster.pk).exists()


def test_proposals_and_different(make_page: MakePage) -> None:
    a, b, c = [make_page(p) for p in "abc"]
    for p in (a, b, c):
        add_frames(p, [bytes(32)])
    label_pair(a, c, PairLabel.Label.DIFFERENT)
    assert build_clusters() == (1, 2)
    assert build_clusters() == (0, 0)
    assert not ClusterMember.objects.filter(page=c).exists()
    label_pair(a, b, PairLabel.Label.DIFFERENT)
    build_clusters()
    assert (
        not ClusterMember.objects.filter(page=a).exists()
        or not ClusterMember.objects.filter(page=b).exists()
    )


def test_cluster_evaluation_and_constraints(make_page: MakePage) -> None:
    a, b, c = [make_page(p) for p in "abc"]
    cluster = Cluster.objects.create()
    for p, status in ((a, "confirmed"), (b, "confirmed"), (c, "rejected")):
        ClusterMember.objects.create(cluster=cluster, page=p, status=status, evidence="manual")
    assert sorted(p.same for p in labeled_pairs()) == [False, False, True]
    label_pair(a, b, PairLabel.Label.DIFFERENT)
    assert not any(p.same for p in labeled_pairs())
    with pytest.raises(ValueError):
        build_clusters()
    with pytest.raises(IntegrityError), transaction.atomic():
        ClusterMember.objects.create(cluster=Cluster.objects.create(), page=a, evidence="manual")
    with pytest.raises(IntegrityError), transaction.atomic():
        ClusterMember.objects.create(cluster=cluster, page=c, evidence="manual")
    cluster.members.all().delete()
    assert Cluster.objects.filter(pk=cluster.pk).exists()


def test_human_merges_existing_clusters(make_page: MakePage) -> None:
    pages = [make_page(p) for p in "abcd"]
    for pair in (pages[:2], pages[2:]):
        cluster = Cluster.objects.create()
        for page in pair:
            ClusterMember.objects.create(
                cluster=cluster, page=page, status="confirmed", evidence="manual"
            )
    label_pair(pages[1], pages[2], PairLabel.Label.SAME)
    assert build_clusters() == (0, 0)
    assert ClusterMember.objects.values("cluster").distinct().count() == 1
    assert Cluster.objects.count() == 1


def test_rejected_never_reproposed(make_page: MakePage) -> None:
    a, b, c = [make_page(p) for p in "abc"]
    cluster = Cluster.objects.create()
    for page, status in ((a, "confirmed"), (b, "confirmed"), (c, "rejected")):
        add_frames(page, [bytes(32)])
        ClusterMember.objects.create(cluster=cluster, page=page, status=status, evidence="manual")
    assert build_clusters() == (0, 0)
    label_pair(a, c, PairLabel.Label.SAME)
    with pytest.raises(ValueError, match="rejected"):
        build_clusters()
    assert ClusterMember.objects.get(cluster=cluster, page=c).status == "rejected"
    out = StringIO()
    call_command("evaluate_threshold", "--max-threshold", "0", stdout=out)
    assert "3 labeled pairs (2 same, 1 different)" in out.getvalue()


def test_ignored_links_and_empty_sha(make_page: MakePage) -> None:
    for name in "ab":
        page = make_page(name)
        Media.objects.create(page=page, kind="poster", url=page.url + "/poster")
        page.raw_extraction = {
            "external_links": [
                "https://t.me/promo",
                "https://telegram.me/promo",
                "tg://resolve?domain=promo",
                42,
            ]
        }
        page.save()
    assert evidence_edges() == []


def test_admin_prefetch(make_page: MakePage) -> None:
    from django.contrib.admin import AdminSite
    from django.db import connection
    from django.test import RequestFactory
    from django.test.utils import CaptureQueriesContext

    from matching.admin import ClusterAdmin

    for i in range(3):
        cluster = Cluster.objects.create()
        ClusterMember.objects.create(cluster=cluster, page=make_page(str(i)), evidence="manual")
    model_admin = ClusterAdmin(Cluster, AdminSite())
    with CaptureQueriesContext(connection) as queries:
        clusters = list(model_admin.get_queryset(RequestFactory().get("/")))
        for cluster in clusters:
            assert model_admin.member_count(cluster) == 1
            assert model_admin.representative_title(cluster)
            assert model_admin.sites(cluster)
            assert model_admin.has_proposed(cluster)
    assert len(queries) == 2


@pytest.mark.parametrize("count", [5, 6])
def test_sha_group_cap_counts_pages(make_page: MakePage, count: int) -> None:
    for i in range(count):
        page = make_page(str(i))
        for j in range(2):
            Media.objects.create(page=page, kind="poster", url=f"{page.url}/{j}", sha256="a" * 64)
    edges = evidence_edges()
    assert len(edges) == (10 if count == 5 else 0)
    assert all(e.evidence == "sha256" for e in edges)


@pytest.mark.parametrize("count", [2, 4])
def test_shared_scene_sets(make_page: MakePage, count: int) -> None:
    for i in range(count):
        page = make_page(str(i))
        page.raw_extraction = {
            "duration_s": 1800,
            "external_links": [f"https://files.test/scene/{j}" for j in range(4)]
            + [f"https://posts.test/{i}"],
        }
        page.save()
    assert evidence_edges() == []
    assert build_clusters() == (0, 0)
    assert build_clusters() == (0, 0)


def test_promotional_links_drop_stale_proposals(make_page: MakePage) -> None:
    cluster = Cluster.objects.create()
    for i, shared in enumerate(([0, 1, 2, 3, 4], [0, 1, 2, 3, 4], [1, 2, 3, 4, 5])):
        page = make_page(str(i))
        page.raw_extraction = {
            "external_links": [f"https://files.test/{j}" for j in shared]
            + [f"https://mirror.test/{i}", f"https://posts.test/{i}"]
        }
        page.save()
        ClusterMember.objects.create(cluster=cluster, page=page, evidence="external_link")
    assert evidence_edges() == []
    build_clusters(dry_run=True)
    assert cluster.members.count() == 3
    build_clusters()
    assert not ClusterMember.objects.exists()
    assert not Cluster.objects.exists()


def test_rebuild_preserves_human_rows(make_page: MakePage) -> None:
    cluster = Cluster.objects.create()
    for i, status in enumerate(("confirmed", "rejected", "proposed")):
        ClusterMember.objects.create(
            cluster=cluster, page=make_page(str(i)), status=status, evidence="manual"
        )
    before = list(cluster.members.exclude(status="proposed").values())
    build_clusters()
    assert list(cluster.members.values()) == before
    assert Cluster.objects.filter(pk=cluster.pk).exists()


@pytest.mark.parametrize(
    ("durations", "pdq", "expected"),
    [
        ([1400, 1550, 1750, 1995], False, 0),
        ([1774, 1488, 1995, 1761], False, 0),
        ([1800, 1800], True, 1),
        ([1800, 1800], False, 0),
        ([None, None], False, 0),
        ([1800, None], False, 0),
        ([1800, 120], True, 1),
        ([1000, 970], False, 0),
        ([1000, 969], False, 0),
        ([2000, 1940], False, 0),
        ([2000, 1939], False, 0),
    ],
)
def test_link_corroboration(
    make_page: MakePage, durations: list[int | None], pdq: bool, expected: int
) -> None:
    for i, duration in enumerate(durations):
        page = make_page(str(i))
        page.raw_extraction = {
            "duration_s": duration,
            "external_links": [f"https://files.test/{j}" for j in range(4)]
            + [f"https://posts.test/{i}"],
        }
        page.save()
        if pdq:
            add_frames(page, [bytes(32)])
    assert sum(e.evidence == "external_link" for e in evidence_edges()) == expected
    assert build_clusters() == ((1, 2) if expected else (0, 0))


@pytest.mark.parametrize("evidence", ["pdq", "sha256"])
def test_duration_never_vetoes_evidence(make_page: MakePage, evidence: str) -> None:
    for i, duration in enumerate((1800, 60)):
        page = make_page(str(i))
        page.raw_extraction = {"duration_s": duration}
        page.save()
        add_frames(page, [bytes(32) if evidence == "pdq" else bytes([i]) * 32])
        if evidence == "sha256":
            Media.objects.filter(page=page).update(sha256="a" * 64)
    assert evidence in {e.evidence for e in evidence_edges()}
    assert build_clusters() == (1, 2)


def test_inline_duration_and_possible_clip(make_page: MakePage) -> None:
    from unittest.mock import Mock

    from django.contrib.admin import AdminSite
    from django.db import connection
    from django.test import RequestFactory
    from django.test.utils import CaptureQueriesContext

    from matching.admin import ClusterMemberInline

    cluster = Cluster.objects.create()
    for i, duration in enumerate((2000, 1940, 1939, 120, None, 9000)):
        page = make_page(str(i))
        page.raw_extraction = {"duration_s": duration}
        page.save()
        ClusterMember.objects.create(
            cluster=cluster,
            page=page,
            evidence="manual",
            status="rejected" if duration == 9000 else "confirmed",
        )
    inline = ClusterMemberInline(Cluster, AdminSite())
    request = RequestFactory().get("/")
    request.user = Mock(is_active=True, is_staff=True, is_superuser=True)
    with CaptureQueriesContext(connection) as queries:
        members = list(inline.get_queryset(request).order_by("pk"))
        assert [inline.duration(m) for m in members] == [
            "33:20",
            "32:20",
            "32:19",
            "02:00",
            "—",
            "150:00",
        ]
        assert [inline.possible_clip(m) for m in members] == [
            False,
            False,
            True,
            True,
            False,
            False,
        ]
    assert len(queries) == 3
    assert {"duration", "possible_clip"} <= set(inline.readonly_fields)


@pytest.mark.parametrize("pdq", [False, True])
def test_rejected_scenes_require_new_evidence(make_page: MakePage, pdq: bool) -> None:
    cluster = Cluster.objects.create()
    for name, duration in (("665", 1774), ("668", 1761)):
        page = make_page(name)
        page.raw_extraction = {
            "duration_s": duration,
            "external_links": ["https://files.test/shared"],
        }
        page.save()
        ClusterMember.objects.create(
            cluster=cluster, page=page, status="rejected", evidence="external_link"
        )
        if pdq:
            add_frames(page, [bytes(32)])
    assert build_clusters() == ((1, 2) if pdq else (0, 0))
    assert cluster.members.filter(status="rejected").count() == 2
    assert set(
        ClusterMember.objects.filter(status="proposed").values_list("evidence", flat=True)
    ) == ({"pdq"} if pdq else set())
    assert build_clusters() == (0, 0)


def test_confirmed_and_rejected_stay_in_separate_clusters(make_page: MakePage) -> None:
    a, b, c = [make_page(name) for name in "abc"]
    old, target = Cluster.objects.create(), Cluster.objects.create()
    ClusterMember.objects.create(cluster=old, page=a, status="confirmed", evidence="manual")
    ClusterMember.objects.create(cluster=old, page=b, status="rejected", evidence="pdq")
    ClusterMember.objects.create(cluster=target, page=b, status="confirmed", evidence="manual")
    for page in (a, b, c):
        add_frames(page, [bytes(32)])
    build_clusters()
    assert not ClusterMember.objects.filter(cluster=old, page=b, status="proposed").exists()
    assert not ClusterMember.objects.filter(cluster=target, page=a).exists()


def test_mirror_paths_and_cap(make_page: MakePage) -> None:
    path = "/imgs/34/99/1a2b3c4d5e6f708_main.jpg"
    a, b = make_page("mirror-a"), make_page("mirror-b")
    for page, host in [(a, "first.test"), (b, "second.test")]:
        Media.objects.create(page=page, kind="poster", url=f"https://{host}{path}?token=1")
    assert {e.evidence for e in evidence_edges()} == {"mirror"}
    assert build_clusters() == (1, 2)
    for i in range(4):
        Media.objects.create(
            page=make_page(f"popular-{i}"), kind="poster", url=f"https://cdn.test{path}"
        )
    assert evidence_edges() == []


def test_mirror_excludes_generic_paths_and_other_media(make_page: MakePage) -> None:
    for page in [make_page("one"), make_page("two")]:
        Media.objects.create(page=page, kind="poster", url="https://cdn.test/default.jpg")
        Media.objects.create(
            page=page, kind="video", url="https://cdn.test/imgs/34/99/1a2b3c4d5e6f708_main.jpg"
        )
    assert evidence_edges() == []


@pytest.mark.parametrize("include_poster", [False, True])
def test_real_mirror_frames_and_poster(make_page: MakePage, include_poster: bool) -> None:
    a, b = make_page("site02"), make_page("site04")
    b.site = Site.objects.create(domain="site04.example", name="Site04")
    b.save()
    base = "https://fastdelivery.site02.example/imgs/34/99/1a2b3c4d5e6f708"
    for i in range(1, 11):
        Media.objects.create(page=a, kind="frame", url=f"{base}_{i}.jpg")
    if include_poster:
        Media.objects.create(page=a, kind="poster", url=f"{base}_main.jpg")
    Media.objects.create(
        page=b,
        kind="poster",
        url="https://imgs.cdn.site04.example/fastdelivery/imgs/34/99/1a2b3c4d5e6f708_main.jpg",
    )
    assert evidence_edges() == [Edge(a.pk, b.pk, "mirror")]
    for i in range(3):
        Media.objects.create(page=make_page(f"cap-{i}"), kind="frame", url=f"{base}_{i + 1}.jpg")
    assert len(evidence_edges()) == 10  # Five distinct pages, despite repeated frames.
    Media.objects.create(page=make_page("over-cap"), kind="poster", url=f"{base}_main.jpg")
    assert evidence_edges() == []


@pytest.mark.parametrize("prefix", ["", "/fastdelivery", "/nested/cdn"])
def test_mirror_prefixes_and_unrelated_ids(make_page: MakePage, prefix: str) -> None:
    a, b, unrelated = [make_page(name) for name in ("a", "b", "unrelated")]
    for page, path, video_id, suffix in [
        (a, "", "1a2b3c4d5e6f708", "main"),
        (b, prefix, "1a2b3c4d5e6f708", "10"),
        (unrelated, prefix, "1a2b3c4d5e6f709", "main"),
    ]:
        Media.objects.create(
            page=page,
            kind="poster" if suffix == "main" else "frame",
            url=f"https://cdn.test{path}/imgs/34/99/{video_id}_{suffix}.jpg?token=1",
        )
    assert evidence_edges() == [Edge(a.pk, b.pk, "mirror")]
