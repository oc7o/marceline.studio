import json
from typing import Any

from django.test import Client

from catalog.models import Creator, Site
from index.models import Media, Page
from matching.copy_extent import CLASSIFIER_VERSION
from matching.models import (
    Cluster,
    ClusterMember,
    ConfidenceSettings,
    Reference,
    ReferenceLeak,
    ReferenceMatch,
)

ITEMS = "/api/v1/review/references/items"


def post_json(client: Client, path: str, data: object) -> Any:
    return client.post(path, json.dumps(data), content_type="application/json")


def make_page(site: Site, slug: str, **fields: Any) -> Page:
    fields.setdefault("title", slug)
    # Scored clearly safe, so its media is listed and served (ADR 0018).
    fields.setdefault("safety_score", 1.0)
    return Page.objects.create(site=site, url=f"https://example.test/{slug}", **fields)


def make_match(reference: Reference, page: Page, **fields: Any) -> ReferenceMatch:
    values: dict[str, Any] = {
        "best_distance": 12,
        "matched_frames": 3,
        "page_frames": 10,
        "best_reference_timestamp_s": 75.0,
        "evidence_frames": 2,
        "matched_seconds": [75, 76],
        "shared_seconds": [3],
        "strength": "strong",
        "band": "high",
        "confidence": 0.8,
        **fields,
    }
    return ReferenceMatch.objects.create(reference=reference, page=page, **values)


def page_ids(response: Any) -> list[int]:
    return [item["page"]["id"] for item in response.json()["items"]]


def walk(client: Client, url: str) -> list[int]:
    """All page ids of a queue, following the cursor one item at a time."""
    ids: list[int] = []
    cursor = None
    while True:
        suffix = f"&cursor={cursor}" if cursor else ""
        data = client.get(f"{url}&limit=1{suffix}").json()
        ids += [item["page"]["id"] for item in data["items"]]
        cursor = data["next_cursor"]
        if cursor is None:
            return ids


def test_confidence_fields_and_thresholds_meta(
    staff_client: Client, reference: Reference, site: Site
) -> None:
    page = make_page(site, "site03")
    make_match(
        reference,
        page,
        best_distance=26,
        evidence_frames=6,
        loose_frames=4,
        common_frames=1,
        common_seconds=[3, 64],
        timeline_score=0.9933,
        consistent_frames=6,
        timeline_reason="clip: 6/6 frames in order, spacing R²=0.97; hover: 2/3 frames in order",
        confidence=0.7616,
        confidence_signals={
            "common": -0.0214,
            "distance": 0.07,
            "duration": 0.05,
            "evidence": 0.443,
            "timeline": 0.1987,
            "corroborated": False,
            "corroboration": 0.0,
            "future_signal": 0.01,
        },
        decided_by="",
    )
    poster = make_page(site, "poster")
    make_match(
        reference, poster, band="review", confidence=0.5, timeline_score=None, strength="weak"
    )

    data = staff_client.get(ITEMS).json()

    assert data["meta"] == {
        "band_thresholds": {"high": 0.75, "review": 0.4, "calibrated": False},
        "hidden_low": 0,
    }
    first, second = data["items"]
    match = first["match"]
    assert {
        key: match[key]
        for key in (
            "band",
            "confidence",
            "timeline_score",
            "consistent_frames",
            "ordered_frames",
            "loose_frames",
            "common_frames",
            "common_seconds",
            "decided_by",
        )
    } == {
        "band": "high",
        "confidence": 0.7616,
        "timeline_score": 0.9933,
        "consistent_frames": 6,
        "ordered_frames": 9,
        "loose_frames": 4,
        "common_frames": 1,
        "common_seconds": ["00:03", "01:04"],
        "decided_by": None,
    }
    assert match["timeline_reason"].startswith("clip: 6/6 frames in order")
    signals = match["confidence_signals"]
    assert [s["key"] for s in signals] == [
        "evidence",
        "distance",
        "timeline",
        "duration",
        "corroboration",
        "common",
        "future_signal",
    ]
    assert signals[0] == {
        "key": "evidence",
        "label": "Evidence frames",
        "value": "6 evidence frames (4 loose)",
        "weight": 0.45,
        "contribution": 0.443,
    }
    assert signals[2]["value"] == "score 0.99"
    assert signals[3]["value"] == "near the original's duration (cluster tolerance)"
    assert signals[4]["value"] == "none"
    assert signals[5]["weight"] == -0.15
    assert signals[6] == {
        "key": "future_signal",
        "label": "Future signal",
        "value": "",
        "weight": None,
        "contribution": 0.01,
    }
    assert second["match"]["timeline_score"] is None
    assert second["match"]["ordered_frames"] is None

    ConfidenceSettings.objects.create(
        pk=1, high_threshold=0.8, review_threshold=0.35, calibrated=True
    )
    assert staff_client.get(ITEMS).json()["meta"]["band_thresholds"] == {
        "high": 0.8,
        "review": 0.35,
        "calibrated": True,
    }


def test_band_order_low_hidden_and_cursor(
    staff_client: Client, reference: Reference, site: Site
) -> None:
    low = make_match(reference, make_page(site, "low"), band="low", confidence=0.3).page
    review_hi = make_match(reference, make_page(site, "rev1"), band="review", confidence=0.7).page
    review_lo = make_match(reference, make_page(site, "rev2"), band="review", confidence=0.45).page
    high = make_match(reference, make_page(site, "high"), band="high", confidence=0.76).page
    tie_a = make_match(reference, make_page(site, "tieA"), band="high", confidence=0.9).page
    tie_b = make_match(reference, make_page(site, "tieB"), band="high", confidence=0.9).page
    leak_only = make_page(site, "leak")
    ReferenceLeak.objects.create(reference=reference, page=leak_only, score=0.95)
    decided = make_match(
        reference, make_page(site, "done"), band="high", confidence=0.99, status="confirmed"
    ).page

    default = staff_client.get(ITEMS)
    assert page_ids(default) == [
        tie_a.pk,
        tie_b.pk,
        high.pk,
        review_hi.pk,
        review_lo.pk,
        leak_only.pk,
    ]
    assert default.json()["meta"]["hidden_low"] == 1

    with_low = staff_client.get(f"{ITEMS}?include_low=true").json()
    assert [i["page"]["id"] for i in with_low["items"]][-2:] == [low.pk, leak_only.pk]
    assert with_low["meta"]["hidden_low"] == 0

    # Proposed first, then decided pairs in the same band order.
    everything = walk(staff_client, f"{ITEMS}?status=all&include_low=true")
    assert everything == [
        tie_a.pk,
        tie_b.pk,
        high.pk,
        review_hi.pk,
        review_lo.pk,
        low.pk,
        leak_only.pk,
        decided.pk,
    ]

    assert page_ids(staff_client.get(f"{ITEMS}?band=low")) == [low.pk]
    assert page_ids(staff_client.get(f"{ITEMS}?band=review&band=high")) == [
        tie_a.pk,
        tie_b.pk,
        high.pk,
        review_hi.pk,
        review_lo.pk,
    ]
    assert page_ids(staff_client.get(f"{ITEMS}?band=review&include_low=true")) == [
        review_hi.pk,
        review_lo.pk,
        low.pk,
    ]
    assert staff_client.get(f"{ITEMS}?band=bogus").status_code == 422

    # Keyset cursor across band boundaries: deciding items already seen never shifts the rest.
    first = staff_client.get(f"{ITEMS}?limit=3&include_low=true").json()
    assert [i["page"]["id"] for i in first["items"]] == [tie_a.pk, tie_b.pk, high.pk]
    ids = [i["match"]["id"] for i in first["items"]]
    confirmed = post_json(
        staff_client,
        "/api/v1/review/references/matches/decide",
        {"ids": ids, "decision": "confirm"},
    )
    assert confirmed.status_code == 200
    second = staff_client.get(f"{ITEMS}?limit=3&include_low=true&cursor={first['next_cursor']}")
    assert page_ids(second) == [review_hi.pk, review_lo.pk, low.pk]
    third = staff_client.get(
        f"{ITEMS}?limit=3&include_low=true&cursor={second.json()['next_cursor']}"
    ).json()
    assert [i["page"]["id"] for i in third["items"]] == [leak_only.pk]
    assert third["next_cursor"] is None


def test_decided_pairs_are_never_hidden_by_band(
    staff_client: Client, reference: Reference, site: Site
) -> None:
    """A confirmed leak re-banded low by a later matching run stays in the confirmed queue."""
    confirmed_low = make_match(
        reference, make_page(site, "kept"), band="low", confidence=0.39, status="confirmed"
    ).page
    rejected_low = make_match(
        reference, make_page(site, "gone"), band="low", confidence=0.2, status="rejected"
    ).page
    proposed_low = make_match(reference, make_page(site, "open"), band="low", confidence=0.3).page

    confirmed = staff_client.get(f"{ITEMS}?status=confirmed").json()
    assert [item["page"]["id"] for item in confirmed["items"]] == [confirmed_low.pk]
    assert confirmed["meta"]["hidden_low"] == 0
    assert page_ids(staff_client.get(f"{ITEMS}?status=confirmed&band=high")) == [confirmed_low.pk]
    assert page_ids(staff_client.get(f"{ITEMS}?status=rejected")) == [rejected_low.pk]
    everything = staff_client.get(f"{ITEMS}?status=all").json()
    assert sorted(item["page"]["id"] for item in everything["items"]) == sorted(
        [confirmed_low.pk, rejected_low.pk]
    )
    # Only the proposed low-band pair is hidden (and counted as hidden).
    assert everything["meta"]["hidden_low"] == 1
    assert proposed_low.pk in page_ids(staff_client.get(f"{ITEMS}?status=all&include_low=true"))


def test_decided_by_filter(staff_client: Client, reference: Reference, site: Site) -> None:
    auto = make_match(
        reference, make_page(site, "auto"), status="confirmed", decided_by="auto"
    ).page
    human = make_match(
        reference, make_page(site, "human"), status="rejected", decided_by="human"
    ).page
    unauthored = make_page(site, "unauthored")
    ReferenceLeak.objects.create(reference=reference, page=unauthored, status="confirmed")
    make_match(reference, make_page(site, "open"))

    assert page_ids(staff_client.get(f"{ITEMS}?status=all&decided_by=auto")) == [auto.pk]
    assert sorted(page_ids(staff_client.get(f"{ITEMS}?status=all&decided_by=human"))) == sorted(
        [human.pk, unauthored.pk]
    )
    [item] = staff_client.get(f"{ITEMS}?status=confirmed&decided_by=auto").json()["items"]
    assert item["match"]["decided_by"] == "auto"
    assert staff_client.get(f"{ITEMS}?decided_by=auto").json()["items"] == []


def test_item_content(staff_client: Client, reference: Reference, site: Site) -> None:
    page = make_page(
        site,
        "v1",
        title="Synthetic Artist mountain sunrise HD",
        raw_extraction={"duration_s": 990},
        availability="playable",
    )
    page.creators.add(reference.creator)
    leak = ReferenceLeak.objects.create(
        reference=reference, page=page, source="suggested", score=0.9
    )
    match = make_match(reference, page)
    cluster = Cluster.objects.create()
    ClusterMember.objects.create(cluster=cluster, page=page, evidence="pdq", status="confirmed")
    ClusterMember.objects.create(cluster=cluster, page=make_page(site, "copy"), evidence="pdq")
    poster = Media.objects.create(page=page, kind="poster", url="https://cdn.example.test/p.jpg")
    frame = Media.objects.create(page=page, kind="frame", url="https://cdn.example.test/f.jpg")
    Media.objects.create(page=page, kind="preview", url="https://cdn.example.test/v.mp4")
    Media.objects.create(page=page, kind="sprite", url="https://cdn.example.test/s.jpg")
    # Created last, yet listed first: a photo *is* the page, so it leads its images (issue #69).
    photo = Media.objects.create(page=page, kind="photo", url="https://n1.test/p_0040.jpg")

    response = staff_client.get(ITEMS)

    assert response.status_code == 200
    assert b"cdn.example.test" not in response.content
    data = response.json()
    assert data["next_cursor"] is None
    [item] = data["items"]
    assert item["key"] == f"r{reference.pk}-p{page.pk}"
    assert item["status"] == "proposed"
    assert item["reference"] == {
        "id": reference.pk,
        "title": reference.title,
        "creator_id": reference.creator_id,
        "creator_name": "Synthetic Artist",
        "creator_names": ["Synthetic Artist"],
        "duration_s": 1000.0,
    }
    assert item["page"]["duration_s"] == 990.0
    assert item["page"]["availability"] == "playable"
    assert item["leak"]["id"] == leak.pk
    assert item["match"]["id"] == match.pk
    assert item["match"]["matched_moments"] == ["01:15", "01:16"]
    assert item["match"]["shared_moments"] == ["00:03"]
    assert item["match"]["best_moment"] == "01:15"
    assert item["signals"] == {
        "score": 0.9,  # stored leak score
        "duration_diff_s": -10.0,
        "duration_ratio": 0.99,
        "title_similarity": 1.0,
        "creator_linked": True,
        "matched_strength": "strong",
        "related_kind": None,
        "related_page_id": None,
    }
    assert item["leak"]["source"] == "suggested"
    assert item["cluster"] == {"id": cluster.pk, "member_count": 2}
    assert item["media"] == [
        {"id": photo.pk, "kind": "photo"},
        {"id": poster.pk, "kind": "poster"},
        {"id": frame.pk, "kind": "frame"},
    ]
    assert item["media_hidden"] is False


def test_cluster_propagated_leak_signals(
    staff_client: Client, reference: Reference, site: Site
) -> None:
    seed, page = make_page(site, "seed"), make_page(site, "member")
    make_match(reference, seed)
    ReferenceLeak.objects.create(
        reference=reference,
        page=page,
        source="cluster",
        score=0.8,
        signals={"matched_page_id": str(seed.pk), "related_kind": "mirror"},
    )

    items = staff_client.get(ITEMS).json()["items"]

    [item] = [i for i in items if i["page"]["id"] == page.pk]
    assert item["leak"]["source"] == "cluster"
    assert item["match"] is None
    assert item["signals"]["score"] == 0.8
    assert item["signals"]["related_kind"] == "mirror"
    assert item["signals"]["related_page_id"] == seed.pk


def test_flagged_page_has_no_media(staff_client: Client, reference: Reference, site: Site) -> None:
    page = make_page(site, "flagged", safety_status="review")
    Media.objects.create(page=page, kind="poster", url="https://cdn.example.test/p.jpg")
    ReferenceLeak.objects.create(reference=reference, page=page)

    [item] = staff_client.get(ITEMS).json()["items"]

    assert item["media"] == []
    assert item["media_hidden"] is True


def test_filters_and_stable_cursor(staff_client: Client, reference: Reference, site: Site) -> None:
    pages = [make_page(site, f"p{i}") for i in range(3)]
    leaks = [
        ReferenceLeak.objects.create(reference=reference, page=page, score=score)
        for page, score in zip(pages, (0.5, 0.9, 0.7), strict=True)
    ]
    other = Reference.objects.create(
        creator=Creator.objects.create(name="Other"), title="x", sha256="b" * 64, frame_fps=1
    )
    make_match(other, pages[0], strength="weak")

    first = staff_client.get(f"{ITEMS}?limit=2&reference_id={reference.pk}").json()
    assert [i["page"]["id"] for i in first["items"]] == [pages[1].pk, pages[2].pk]
    assert first["next_cursor"]

    decided = post_json(
        staff_client,
        "/api/v1/review/references/leaks/decide",
        {"ids": [leaks[1].pk], "decision": "confirm"},
    )
    assert decided.status_code == 200

    second = staff_client.get(
        f"{ITEMS}?limit=2&reference_id={reference.pk}&cursor={first['next_cursor']}"
    ).json()
    assert [i["page"]["id"] for i in second["items"]] == [pages[0].pk]
    assert second["next_cursor"] is None

    confirmed = staff_client.get(f"{ITEMS}?status=confirmed").json()["items"]
    assert [i["page"]["id"] for i in confirmed] == [pages[1].pk]
    assert len(staff_client.get(f"{ITEMS}?status=all").json()["items"]) == 4
    assert len(staff_client.get(f"{ITEMS}?creator_id={other.creator_id}").json()["items"]) == 1
    other_site = Site.objects.create(domain="other.test")
    assert staff_client.get(f"{ITEMS}?site_id={other_site.pk}").json()["items"] == []
    assert staff_client.get(f"{ITEMS}?cursor=nonsense").status_code == 400


def test_pair_decisions_stay_consistent(
    staff_client: Client, reference: Reference, site: Site
) -> None:
    page = make_page(site, "pair")
    leak = ReferenceLeak.objects.create(reference=reference, page=page)
    match = make_match(reference, page)
    before = (leak.updated_at, match.updated_at)

    response = post_json(
        staff_client,
        "/api/v1/review/references/leaks/decide",
        {"ids": [leak.pk], "decision": "confirm"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "decision": "confirm",
        "leak_ids": [leak.pk],
        "match_ids": [match.pk],
    }
    leak.refresh_from_db()
    match.refresh_from_db()
    assert (leak.status, match.status) == ("confirmed", "confirmed")
    assert leak.updated_at > before[0]
    assert match.updated_at > before[1]

    again = post_json(
        staff_client,
        "/api/v1/review/references/matches/decide",
        {"ids": [match.pk], "decision": "confirm"},
    )
    assert again.json() == {"decision": "confirm", "leak_ids": [], "match_ids": []}

    conflict = post_json(
        staff_client,
        "/api/v1/review/references/matches/decide",
        {"ids": [match.pk], "decision": "reject"},
    )
    assert conflict.status_code == 409
    assert {(c["kind"], c["id"]) for c in conflict.json()["conflicts"]} == {
        ("leak", leak.pk),
        ("match", match.pk),
    }
    leak.refresh_from_db()
    assert leak.status == "confirmed"

    reset = post_json(
        staff_client,
        "/api/v1/review/references/matches/decide",
        {"ids": [match.pk], "decision": "reset"},
    )
    assert reset.status_code == 200
    rejected = post_json(
        staff_client,
        "/api/v1/review/references/matches/decide",
        {"ids": [match.pk], "decision": "reject"},
    )
    assert rejected.status_code == 200
    leak.refresh_from_db()
    assert leak.status == "rejected"


def test_bulk_decisions_and_unknown_ids(
    staff_client: Client, reference: Reference, site: Site
) -> None:
    leaks = [
        ReferenceLeak.objects.create(reference=reference, page=make_page(site, f"b{i}"))
        for i in range(3)
    ]
    ids = [leak.pk for leak in leaks]
    path = "/api/v1/review/references/leaks/decide"

    missing = post_json(staff_client, path, {"ids": [*ids, 999999], "decision": "reject"})
    assert missing.status_code == 404
    assert not ReferenceLeak.objects.exclude(status="proposed").exists()

    response = post_json(staff_client, path, {"ids": ids, "decision": "reject"})
    assert response.status_code == 200
    assert sorted(response.json()["leak_ids"]) == sorted(ids)
    assert set(ReferenceLeak.objects.values_list("status", flat=True)) == {"rejected"}
    assert post_json(staff_client, path, {"ids": [], "decision": "reject"}).status_code == 422


def test_reject_remaining(staff_client: Client, reference: Reference, site: Site) -> None:
    plain = [
        ReferenceLeak.objects.create(reference=reference, page=make_page(site, f"s{i}"))
        for i in range(2)
    ]
    matched_page = make_page(site, "matched")
    matched_leak = ReferenceLeak.objects.create(reference=reference, page=matched_page)
    match = make_match(reference, matched_page)
    kept = ReferenceLeak.objects.create(
        reference=reference, page=make_page(site, "kept"), status="confirmed"
    )
    path = f"/api/v1/review/references/{reference.pk}/reject-remaining"

    summary = staff_client.get("/api/v1/review/references").json()
    assert summary[0]["proposed"] == 3
    assert summary[0]["proposed_suggestions"] == 2
    assert summary[0]["confirmed"] == 1

    response = post_json(staff_client, path, {})
    assert response.status_code == 200
    assert sorted(response.json()["leak_ids"]) == sorted(leak.pk for leak in plain)
    matched_leak.refresh_from_db()
    assert matched_leak.status == "proposed"

    response = post_json(staff_client, path, {"include_matches": True})
    assert response.json() == {
        "decision": "reject",
        "leak_ids": [matched_leak.pk],
        "match_ids": [match.pk],
    }
    kept.refresh_from_db()
    assert kept.status == "confirmed"
    unknown = "/api/v1/review/references/999999/reject-remaining"
    assert post_json(staff_client, unknown, {}).status_code == 404
    assert staff_client.get("/api/v1/review/counts").json()["references"] == 0


def test_copy_extent_fields(staff_client: Client, reference: Reference, site: Site) -> None:
    clip = make_page(site, "clip")
    make_match(
        reference,
        clip,
        copy_extent="excerpt",
        copy_extent_reason="page_shorter",
        copy_span_s=101.0,
        copy_coverage=0.0281,
        copy_extent_version=CLASSIFIER_VERSION,
    )
    full = make_page(site, "full")
    make_match(
        reference,
        full,
        confidence=0.7,
        copy_extent="full_copy",
        copy_extent_reason="full_span",
        copy_span_s=3500.0,
        copy_coverage=0.9722,
        copy_extent_version=CLASSIFIER_VERSION,
    )
    stale = make_page(site, "stale")
    make_match(
        reference, stale, confidence=0.6, copy_extent="full_copy", copy_extent_reason="full_span"
    )

    items = {item["page"]["id"]: item["match"] for item in staff_client.get(ITEMS).json()["items"]}

    keys = ("copy_extent", "copy_extent_reason", "copy_coverage", "exception_review_required")
    assert {key: items[clip.pk][key] for key in keys} == {
        "copy_extent": "excerpt",
        "copy_extent_reason": "page_shorter",
        "copy_coverage": 0.0281,
        "exception_review_required": True,
    }
    assert items[full.pk]["copy_extent"] == "full_copy"
    assert items[full.pk]["exception_review_required"] is False
    # Written by another classifier version (here: never classified): not trusted, not shown.
    assert {key: items[stale.pk][key] for key in keys[:2]} == {
        "copy_extent": "unknown",
        "copy_extent_reason": "not_classified",
    }
    assert items[stale.pk]["exception_review_required"] is True


def test_a_decision_is_dated_and_a_reset_clears_the_date(
    staff_client: Client, reference: Reference, site: Site
) -> None:
    """`ReferenceMatch.decided_at` is when *this* decision was taken (issue #142).

    A notice draft snapshots it as its human-review date and the rendered notice names it, so it may
    not be `updated_at` — which an extent classification moves months later — and a row put back to
    `proposed` has no decision to date.
    """
    page = make_page(site, "dated")
    match = make_match(reference, page)
    assert match.decided_at is None

    post_json(
        staff_client,
        "/api/v1/review/references/matches/decide",
        {"ids": [match.pk], "decision": "confirm"},
    )
    match.refresh_from_db()
    decided = match.decided_at

    assert decided is not None

    post_json(
        staff_client,
        "/api/v1/review/references/matches/decide",
        {"ids": [match.pk], "decision": "reset"},
    )
    match.refresh_from_db()

    assert match.decided_at is None
