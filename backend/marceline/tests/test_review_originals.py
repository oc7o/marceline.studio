from typing import Any

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from catalog.models import Creator, Site
from index.models import Page
from matching.creators import set_creators
from matching.models import (
    Cluster,
    Reference,
    ReferenceFrameHash,
    ReferenceLeak,
    ReferenceMatch,
)

ORIGINALS = "/api/v1/review/originals"


def make_match(reference: Reference, page: Page, **fields: Any) -> ReferenceMatch:
    values: dict[str, Any] = {
        "best_distance": 10,
        "matched_frames": 3,
        "page_frames": 5,
        "best_reference_timestamp_s": 75.0,
        "evidence_frames": 2,
        "matched_seconds": [76, 75],
        "shared_seconds": [3],
        "strength": "strong",
        **fields,
    }
    return ReferenceMatch.objects.create(reference=reference, page=page, **values)


def frames(reference: Reference, count: int, shared: int) -> None:
    ReferenceFrameHash.objects.bulk_create(
        ReferenceFrameHash(
            reference=reference,
            frame_index=index,
            timestamp_s=index,
            pdq=bytes([index]) * 32,
            quality=100,
            dihedral=dihedral,
            shared=index < shared,
        )
        for index in range(count)
        for dihedral in (0, 1)
    )


def test_requires_staff(client: Client, user_client: Client, reference: Reference) -> None:
    for path in (ORIGINALS, f"{ORIGINALS}/{reference.pk}"):
        assert client.get(path).status_code == 401
        assert user_client.get(path).status_code == 403


def test_creator_names_list_every_performer(staff_client: Client, reference: Reference) -> None:
    set_creators(reference, [Creator.objects.create(name="Carla Bremer")])
    [row] = staff_client.get(ORIGINALS).json()
    # creator_name stays the primary creator (rights holder); creator_names is the full cast.
    assert row["creator_name"] == "Synthetic Artist"
    assert row["creator_names"] == ["Synthetic Artist", "Carla Bremer"]
    detail = staff_client.get(f"{ORIGINALS}/{reference.pk}").json()
    assert detail["original"]["creator_names"] == ["Synthetic Artist", "Carla Bremer"]


def test_list_counts(staff_client: Client, reference: Reference, site: Site) -> None:
    frames(reference, 5, shared=2)
    pages = [Page.objects.create(site=site, url=f"https://example.test/{i}") for i in range(4)]
    cluster = Cluster.objects.create()
    make_match(reference, pages[0], status="confirmed", cluster=cluster)
    make_match(reference, pages[1], strength="weak", cluster=cluster)
    make_match(reference, pages[2], strength="weak", status="rejected")
    ReferenceLeak.objects.create(reference=reference, page=pages[0], status="confirmed")
    ReferenceLeak.objects.create(reference=reference, page=pages[3], status="rejected")
    ReferenceLeak.objects.create(reference=reference, page=pages[2])

    response = staff_client.get(ORIGINALS)

    assert response.status_code == 200
    [row] = response.json()
    assert row["id"] == reference.pk
    assert row["creator_name"] == "Synthetic Artist"
    assert row["title"] == "Synthetic Artist mountain sunrise"
    assert row["duration_s"] == 1000
    assert (row["frames"], row["shared_frames"]) == (5, 2)
    assert (row["matches_strong"], row["matches_weak"]) == (1, 2)
    assert row["matches"] == {"proposed": 1, "confirmed": 1, "rejected": 1}
    assert row["leaks"] == {"proposed": 1, "confirmed": 1, "rejected": 1}
    assert row["linked_clusters"] == 1


def test_detail_pairs_and_misses(staff_client: Client, reference: Reference, site: Site) -> None:
    found = Page.objects.create(site=site, url="https://example.test/found", title="found")
    missed = Page.objects.create(
        site=site, url="https://example.test/missed", title="missed", availability="removed"
    )
    conflict = Page.objects.create(site=site, url="https://example.test/conflict")
    make_match(reference, found, status="confirmed")
    ReferenceLeak.objects.create(reference=reference, page=missed, status="confirmed")
    make_match(reference, conflict, status="rejected", strength="weak")
    ReferenceLeak.objects.create(
        reference=reference, page=conflict, status="confirmed", source="suggested", score=0.5
    )

    response = staff_client.get(f"{ORIGINALS}/{reference.pk}")

    assert response.status_code == 200
    data = response.json()
    assert data["original"]["id"] == reference.pk
    assert [(pair["page"]["id"], pair["status"]) for pair in data["pairs"]] == [
        (conflict.pk, "conflict"),
        (found.pk, "confirmed"),
        (missed.pk, "confirmed"),
    ]
    found_pair = data["pairs"][1]
    assert found_pair["page"] == {
        "id": found.pk,
        "site_id": site.pk,
        "site": "example.test",
        "title": "found",
        "availability": "unknown",
        "safety_status": "clear",
    }
    assert found_pair["match"]["best_moment"] == "01:15"
    assert found_pair["match"]["matched_moments"] == ["01:15", "01:16"]
    assert found_pair["match"]["shared_moments"] == ["00:03"]
    assert found_pair["leak"] is None
    assert data["pairs"][2]["match"] is None
    assert data["pairs"][0]["leak"]["source"] == "suggested"
    assert b"https://example.test" not in response.content
    m2 = data["m2"]
    # The conflict page is confirmed (leak) and has its own weak hit: found, and a false match.
    assert (m2["confirmed"], m2["found_strong"], m2["found_weak"]) == (3, 1, 1)
    assert (m2["false_matches"], m2["reviewed_matches"]) == (1, 2)
    [miss] = m2["misses"]
    assert (miss["page_id"], miss["site"], miss["title"]) == (missed.pk, "example.test", "missed")
    assert miss["reason"] == "page removed; no FrameHash rows; media: none"


def test_detail_unknown(staff_client: Client, db: None) -> None:
    assert staff_client.get(f"{ORIGINALS}/999999").status_code == 404


def test_list_query_count_does_not_grow_with_originals(
    staff_client: Client, reference: Reference, site: Site
) -> None:
    """The list reads the frame hashes exactly once, for any number of originals.

    Measured on the development database (44 originals, 614,360 reference frame hashes): the two
    separate `Count(distinct=True)` aggregates this used to run cost 308 + 238 ms of the
    endpoint's 609 ms, because each one scanned the whole frame-hash table; one subquery pass
    costs 189 ms of 205 ms, with a byte-identical response. Neither a second pass nor a
    per-original query shows up as a wrong number, only as a slow screen, so what is pinned is
    how often that table is touched.
    """
    frames(reference, 4, shared=1)
    with CaptureQueriesContext(connection) as first:
        assert staff_client.get(ORIGINALS).status_code == 200
    for index in range(3):
        more = Reference.objects.create(
            creator=reference.creator,
            title=f"another {index}",
            duration_s=100,
            sha256=f"{index}" * 64,
            frame_fps=1,
        )
        frames(more, 4, shared=1)
        page = Page.objects.create(site=site, url=f"https://example.test/o{index}", title="t")
        make_match(more, page)
    with CaptureQueriesContext(connection) as second:
        rows = staff_client.get(ORIGINALS).json()
    assert len(rows) == 4
    assert all(row["frames"] == 4 and row["shared_frames"] == 1 for row in rows)
    assert len(second) == len(first)
    for queries in (first, second):
        touching_frames = [q for q in queries if "matching_referenceframehash" in q["sql"]]
        assert len(touching_frames) == 1
