import json
from datetime import timedelta
from typing import Any

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog.models import Site
from crawler.models import CrawlTask
from crawler.services import RECHECK_PRIORITY
from index.models import Media, Page
from matching.copy_extent import CLASSIFIER_VERSION
from matching.models import (
    Cluster,
    ClusterMember,
    PairLabel,
    Reference,
    ReferenceLeak,
    ReferenceMatch,
)

DECIDE = "/api/v1/review/clusters/members/decide"


def post_json(client: Client, path: str, data: object) -> Any:
    return client.post(path, json.dumps(data), content_type="application/json")


def page(site: Site, slug: str, duration: float | None = None) -> Page:
    raw = {"duration_s": duration} if duration is not None else {}
    return Page.objects.create(
        site=site, url=f"https://example.test/{slug}", title=slug, raw_extraction=raw
    )


def member(cluster: Cluster, page: Page, status: str = "proposed", **fields: Any) -> ClusterMember:
    return ClusterMember.objects.create(
        cluster=cluster, page=page, status=status, evidence="pdq", **fields
    )


def test_queue_lists_clusters_with_proposals(staff_client: Client, site: Site) -> None:
    open_cluster, done = Cluster.objects.create(), Cluster.objects.create()
    full = member(open_cluster, page(site, "full", 1000), "confirmed", min_distance=0)
    clip = member(open_cluster, page(site, "clip", 300), borderline=True, min_distance=40)
    gone = member(open_cluster, page(site, "gone", 5000), "rejected")
    member(done, page(site, "done"), "confirmed")

    response = staff_client.get("/api/v1/review/clusters", {"show": "all"})

    assert response.status_code == 200
    data = response.json()
    assert data["next_cursor"] is None
    [cluster] = data["items"]
    assert cluster["id"] == open_cluster.pk
    assert (cluster["member_count"], cluster["proposed_count"], cluster["borderline_count"]) == (
        2,
        1,
        1,
    )
    assert [m["id"] for m in cluster["members"]] == [full.pk, clip.pk, gone.pk]
    by_id = {m["id"]: m for m in cluster["members"]}
    assert by_id[clip.pk]["possible_clip"] is True
    assert by_id[clip.pk]["borderline"] is True
    assert by_id[clip.pk]["min_distance"] == 40
    assert by_id[full.pk]["possible_clip"] is False
    assert by_id[clip.pk]["page"]["url"] == "https://example.test/clip"
    assert staff_client.get("/api/v1/review/counts").json()["clusters"] == 1


def test_mirror_evidence_is_shown(staff_client: Client, site: Site) -> None:
    cluster = Cluster.objects.create()
    ClusterMember.objects.create(cluster=cluster, page=page(site, "m"), evidence="mirror")

    [item] = staff_client.get("/api/v1/review/clusters", {"show": "all"}).json()["items"]

    assert item["members"][0]["evidence"] == "mirror"


def test_member_decisions(staff_client: Client, site: Site) -> None:
    cluster = Cluster.objects.create()
    member(cluster, page(site, "a"), "confirmed")
    b, c = member(cluster, page(site, "b")), member(cluster, page(site, "c"))

    response = post_json(staff_client, DECIDE, {"ids": [b.pk, c.pk], "decision": "confirm"})
    assert response.status_code == 200
    assert response.json() == {"decision": "confirm", "member_ids": [b.pk, c.pk], "notes": []}
    assert set(cluster.members.values_list("status", flat=True)) == {"confirmed"}

    conflict = post_json(staff_client, DECIDE, {"ids": [b.pk], "decision": "reject"})
    assert conflict.status_code == 409
    assert conflict.json()["conflicts"][0]["id"] == b.pk

    assert post_json(staff_client, DECIDE, {"ids": [b.pk], "decision": "reset"}).status_code == 200
    assert post_json(staff_client, DECIDE, {"ids": [b.pk], "decision": "reject"}).status_code == 200
    b.refresh_from_db()
    assert b.status == "rejected"
    unknown = post_json(staff_client, DECIDE, {"ids": [999999], "decision": "reject"})
    assert unknown.status_code == 404


def test_service_conflict_is_409(staff_client: Client, site: Site) -> None:
    cluster = Cluster.objects.create()
    a, b = page(site, "a"), page(site, "b")
    member(cluster, a, "confirmed")
    proposed = member(cluster, b)
    PairLabel.objects.create(page_a=a, page_b=b, label="different")

    response = post_json(staff_client, DECIDE, {"ids": [proposed.pk], "decision": "confirm"})

    assert response.status_code == 409
    assert "DIFFERENT" in response.json()["detail"]
    proposed.refresh_from_db()
    assert proposed.status == "proposed"


def test_reset_rejected_member_with_other_active_membership(
    staff_client: Client, site: Site
) -> None:
    shared = page(site, "shared")
    old, new = Cluster.objects.create(), Cluster.objects.create()
    rejected = member(old, shared, "rejected")
    member(new, shared, "confirmed")

    response = post_json(staff_client, DECIDE, {"ids": [rejected.pk], "decision": "reset"})

    assert response.status_code == 409
    rejected.refresh_from_db()
    assert rejected.status == "rejected"


def test_merge(staff_client: Client, site: Site) -> None:
    target, source = Cluster.objects.create(), Cluster.objects.create()
    member(target, page(site, "a"))
    member(source, page(site, "b"))

    response = post_json(
        staff_client,
        "/api/v1/review/clusters/merge",
        {"target_id": target.pk, "source_ids": [source.pk]},
    )

    assert response.status_code == 200
    assert response.json() == {"id": target.pk, "member_count": 2}
    assert not Cluster.objects.filter(pk=source.pk).exists()
    missing = post_json(
        staff_client, "/api/v1/review/clusters/merge", {"target_id": target.pk, "source_ids": [0]}
    )
    assert missing.status_code == 404


def test_merge_conflict_is_409(staff_client: Client, site: Site) -> None:
    target, source = Cluster.objects.create(), Cluster.objects.create()
    shared = page(site, "shared")
    member(target, shared, "confirmed")
    member(source, shared, "rejected")

    response = post_json(
        staff_client,
        "/api/v1/review/clusters/merge",
        {"target_id": target.pk, "source_ids": [source.pk]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "A page is both CONFIRMED and REJECTED"
    assert Cluster.objects.filter(pk=source.pk).exists()


def test_move_member(staff_client: Client, site: Site) -> None:
    first, second = Cluster.objects.create(), Cluster.objects.create()
    moving = member(first, page(site, "a"))
    member(second, page(site, "b"))

    response = post_json(
        staff_client,
        f"/api/v1/review/clusters/members/{moving.pk}/move",
        {"cluster_id": second.pk},
    )

    assert response.status_code == 200
    assert response.json() == {"id": second.pk, "member_count": 2}
    assert not Cluster.objects.filter(pk=first.pk).exists()


# --- Linked originals, provenance, filters, rechecks ---

QUEUE = "/api/v1/review/clusters"


def match(reference: Reference, page: Page, strength: str = "strong", **fields: Any) -> None:
    ReferenceMatch.objects.create(
        reference=reference,
        page=page,
        best_distance=4,
        matched_frames=3,
        page_frames=10,
        best_reference_timestamp_s=12,
        evidence_frames=3 if strength == "strong" else 1,
        strength=strength,
        **fields,
    )


def test_linked_originals_and_member_badges(
    staff_client: Client, site: Site, reference: Reference
) -> None:
    cluster = Cluster.objects.create()
    strong, mirror, gone = page(site, "strong"), page(site, "mirror"), page(site, "gone")
    member(cluster, strong, "confirmed")
    member(cluster, mirror)
    member(cluster, gone, "rejected")
    # A clip of the original stays in its cluster; the review shows it as an excerpt (issue #62).
    match(
        reference,
        strong,
        copy_extent="excerpt",
        copy_extent_reason="page_shorter",
        copy_extent_version=CLASSIFIER_VERSION,
    )
    ReferenceLeak.objects.create(reference=reference, page=mirror, source="cluster", score=0.8)
    # Inactive member and rejected pairs never link the cluster.
    other = Reference.objects.create(
        creator=reference.creator, title="Other original", sha256="b" * 64, frame_fps=1
    )
    ReferenceLeak.objects.create(reference=other, page=gone, status="confirmed")
    ReferenceLeak.objects.create(reference=other, page=mirror, status="rejected")

    [item] = staff_client.get(QUEUE).json()["items"]

    [linked] = item["linked_originals"]
    assert linked["reference"]["id"] == reference.pk
    assert linked["reference"]["creator_name"] == "Synthetic Artist"
    assert linked["pages"] == [
        {
            "page_id": strong.pk,
            "status": "proposed",
            "match_strength": "strong",
            "copy_extent": "excerpt",
            "leak_status": None,
            "leak_source": None,
        },
        {
            "page_id": mirror.pk,
            "status": "proposed",
            "match_strength": None,
            "copy_extent": None,
            "leak_status": "proposed",
            "leak_source": "cluster",
        },
    ]
    by_page = {m["page"]["id"]: m for m in item["members"]}
    assert [o["reference_id"] for o in by_page[mirror.pk]["originals"]] == [reference.pk, other.pk]
    assert by_page[mirror.pk]["originals"][1]["status"] == "rejected"
    assert by_page[strong.pk]["originals"][0]["match_status"] == "proposed"
    assert by_page[strong.pk]["originals"][0]["copy_extent"] == "excerpt"
    assert by_page[mirror.pk]["originals"][0]["copy_extent"] is None
    assert by_page[gone.pk]["originals"][0]["leak_status"] == "confirmed"


def test_linked_filter_reference_filter_and_order(
    staff_client: Client, site: Site, reference: Reference
) -> None:
    plain = Cluster.objects.create()
    member(plain, page(site, "plain"))
    linked = Cluster.objects.create()
    linked_page = page(site, "linked")
    member(linked, linked_page)
    match(reference, linked_page, "weak")
    rejected_only = Cluster.objects.create()
    rejected_page = page(site, "rejected-pair")
    member(rejected_only, rejected_page)
    match(reference, rejected_page, status="rejected")

    default = staff_client.get(QUEUE).json()
    assert [c["id"] for c in default["items"]] == [linked.pk, plain.pk, rejected_only.pk]
    linked_only = staff_client.get(QUEUE, {"show": "linked"}).json()
    assert [c["id"] for c in linked_only["items"]] == [linked.pk]
    everything = staff_client.get(QUEUE, {"show": "all", "limit": "2"}).json()
    assert [c["id"] for c in everything["items"]] == [linked.pk, plain.pk]
    assert everything["next_cursor"] == f"0:{plain.pk}"
    rest = staff_client.get(QUEUE, {"show": "all", "cursor": everything["next_cursor"]}).json()
    assert [c["id"] for c in rest["items"]] == [rejected_only.pk]
    assert rest["items"][0]["linked_originals"] == []
    assert (
        staff_client.get(QUEUE, {"show": "all", "cursor": f"1:{linked.pk}"}).json()["items"][0][
            "id"
        ]
        == plain.pk
    )
    by_reference = staff_client.get(
        QUEUE, {"show": "all", "reference_id": str(reference.pk)}
    ).json()
    assert [c["id"] for c in by_reference["items"]] == [linked.pk]
    assert staff_client.get(QUEUE, {"reference_id": 999999}).json()["items"] == []
    assert staff_client.get(QUEUE, {"cursor": "nope"}).status_code == 400


def test_member_provenance_is_derived(staff_client: Client, site: Site) -> None:
    cluster = Cluster.objects.create()
    a, b, c, d, e = (page(site, slug) for slug in "abcde")
    labelled = ClusterMember.objects.create(
        cluster=cluster, page=a, status="confirmed", evidence="manual"
    )
    ClusterMember.objects.create(cluster=cluster, page=b, status="confirmed", evidence="manual")
    PairLabel.objects.create(page_a=a, page_b=b, label="same")
    by_hand = ClusterMember.objects.create(
        cluster=cluster, page=c, status="confirmed", evidence="manual"
    )
    reviewed = member(cluster, d, "confirmed")
    proposed = ClusterMember.objects.create(cluster=cluster, page=e, evidence="sha256")

    [item] = staff_client.get(QUEUE, {"show": "all"}).json()["items"]

    provenance = {m["id"]: (m["provenance"], m["evidence"]) for m in item["members"]}
    assert provenance[labelled.pk] == ("pair_label", "manual")
    assert provenance[by_hand.pk] == ("reviewer", "manual")
    assert provenance[reviewed.pk] == ("reviewer", "pdq")
    assert provenance[proposed.pk] == ("system", "sha256")


def test_reset_confirmed_member_notes_pair_label(staff_client: Client, site: Site) -> None:
    cluster = Cluster.objects.create()
    a, b, c = page(site, "a"), page(site, "b"), page(site, "c")
    labelled = ClusterMember.objects.create(
        cluster=cluster, page=a, status="confirmed", evidence="manual"
    )
    ClusterMember.objects.create(cluster=cluster, page=b, status="confirmed", evidence="manual")
    PairLabel.objects.create(page_a=a, page_b=b, label="same")
    reviewed = member(cluster, c, "confirmed")

    response = post_json(
        staff_client, DECIDE, {"ids": [labelled.pk, reviewed.pk], "decision": "reset"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["member_ids"] == [labelled.pk, reviewed.pk]
    [note] = data["notes"]
    assert f"Member {labelled.pk}" in note and "pair label" in note
    assert set(
        ClusterMember.objects.filter(pk__in=(labelled.pk, reviewed.pk)).values_list(
            "status", flat=True
        )
    ) == {"proposed"}


def test_member_pages_carry_availability(staff_client: Client, site: Site) -> None:
    cluster = Cluster.objects.create()
    checked_at = timezone.now() - timedelta(hours=3)
    online = Page.objects.create(
        site=site,
        url="https://example.test/online",
        availability="playable",
        availability_checked_at=checked_at,
    )
    member(cluster, online)
    member(cluster, page(site, "never"))

    [item] = staff_client.get(QUEUE, {"show": "all"}).json()["items"]

    pages = {m["page"]["url"]: m["page"] for m in item["members"]}
    assert pages[online.url]["availability"] == "playable"
    assert pages[online.url]["availability_checked_at"] is not None
    assert pages["https://example.test/never"]["availability"] == "unknown"
    assert pages["https://example.test/never"]["availability_checked_at"] is None


def test_cluster_recheck_queues_active_members(staff_client: Client, site: Site) -> None:
    cluster = Cluster.objects.create()
    active = page(site, "active")
    removed = Page.objects.create(
        site=site, url="https://example.test/removed", availability="removed"
    )
    member(cluster, active)
    member(cluster, removed, "confirmed")
    member(cluster, page(site, "rejected"), "rejected")

    response = staff_client.post(f"/api/v1/review/clusters/{cluster.pk}/recheck")

    assert response.status_code == 200
    assert response.json() == {
        "queued": sorted([active.pk, removed.pk]),
        "in_progress": [],
        "skipped": [],
    }
    tasks = CrawlTask.objects.filter(status="pending", kind="detail")
    assert set(tasks.values_list("url", flat=True)) == {active.url, removed.url}
    assert set(tasks.values_list("priority", flat=True)) == {RECHECK_PRIORITY}
    assert staff_client.post("/api/v1/review/clusters/999999/recheck").status_code == 404


def test_queue_query_count_does_not_grow_with_clusters(staff_client: Client, site: Site) -> None:
    """The images of every cluster on the page are looked up in one query, not one per cluster.

    `media_refs` joins `index_media` (256k rows) to `index_page` (211 MB); per cluster that was
    2.4 ms each. Measured on the development database the queue went from 23 queries and 81 ms
    to 14 and 60 ms, with a byte-identical response.
    """

    def queue_with(clusters: int) -> int:
        for index in range(clusters):
            cluster = Cluster.objects.create()
            for slot in range(3):
                page_row = page(site, f"c{clusters}-{index}-{slot}", 100)
                # Media is only listed for a page the gate cleared (ADR 0018).
                Page.objects.filter(pk=page_row.pk).update(safety_score=1.0)
                Media.objects.create(
                    page=page_row,
                    kind=Media.Kind.POSTER,
                    url=f"https://cdn.test/{clusters}-{index}-{slot}.jpg",
                )
                member(cluster, page_row)
        with CaptureQueriesContext(connection) as queries:
            items = staff_client.get(QUEUE, {"show": "all"}).json()["items"]
        assert len(items) >= clusters
        assert all(m["media"] for item in items for m in item["members"])
        return len(queries)

    assert queue_with(1) == queue_with(4)
