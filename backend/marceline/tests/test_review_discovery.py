import json
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from catalog.models import Site
from crawler.models import CrawlTask, SearchCall, SourceCandidate

CANDIDATES = "/api/v1/review/discovery/candidates"
DECIDE = "/api/v1/review/discovery/candidates/decide"
SEARCH_CALLS = "/api/v1/review/discovery/search-calls"


def post_json(client: Client, path: str, data: object) -> Any:
    return client.post(path, json.dumps(data), content_type="application/json")


def candidate(domain: str, **fields: Any) -> SourceCandidate:
    return SourceCandidate.objects.create(domain=domain, **fields)


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", CANDIDATES), ("get", SEARCH_CALLS), ("post", DECIDE)],
)
def test_requires_staff(client: Client, user_client: Client, method: str, path: str) -> None:
    body = json.dumps({"ids": [1], "action": "reject"})
    for http, expected in ((client, 401), (user_client, 403)):
        call = getattr(http, method)
        response = (
            call(path, body, content_type="application/json") if method == "post" else call(path)
        )
        assert response.status_code == expected


def test_list_sorting_filters_and_text_evidence(staff_client: Client, db: None) -> None:
    candidate("few.test", distinct_creators=1, search_hits=9)
    top = candidate(
        "top.test",
        distinct_creators=3,
        search_hits=1,
        kind="tube",
        cms_guess="kvs",
        has_search=True,
        video_links_seen=12,
        robots_allows=True,
        evidence={
            "queries": [
                {"query": "artist leak", "rank": 2, "url": "https://top.test/v", "title": "T"},
                "garbage",
            ],
            "linking_pages": ["https://example.test/page", 5],
            "redirects_to": "https://www.top.test/",
        },
        notes="looks like a video site",
    )
    candidate("many.test", distinct_creators=3, search_hits=4, blocked_reason="challenge")
    candidate("done.test", status="rejected", distinct_creators=9)

    data = staff_client.get(CANDIDATES).json()

    assert [item["domain"] for item in data["items"]] == ["many.test", "top.test", "few.test"]
    assert data["total"] == 3
    assert data["status_counts"] == {
        "proposed": 3,
        "approved": 0,
        "rejected": 1,
        "hoster": 0,
        "ignored": 0,
        "torrent": 0,
        "onboarded": 0,
    }
    assert data["cms_values"] == ["kvs", "unknown"]
    item = data["items"][1]
    assert item["id"] == top.pk
    assert item["evidence"] == {
        "queries": [{"query": "artist leak", "rank": 2, "url": "https://top.test/v", "title": "T"}],
        "linking_pages": ["https://example.test/page"],
        "redirects_to": "https://www.top.test/",
        "mirror_of": None,
    }
    assert item["notes"] == "looks like a video site"
    assert (item["has_search"], item["video_links_seen"], item["robots_allows"]) == (True, 12, True)

    def domains(**query: str) -> list[str]:
        response = staff_client.get(CANDIDATES, query)
        assert response.status_code == 200
        return [row["domain"] for row in response.json()["items"]]

    assert domains(kind="tube") == ["top.test"]
    assert domains(cms_guess="kvs") == ["top.test"]
    assert domains(blocked="true") == ["many.test"]
    assert domains(blocked="false") == ["top.test", "few.test"]
    assert domains(status="rejected") == ["done.test"]
    assert len(domains(status="all")) == 4


def test_approve_creates_disabled_site(staff_client: Client, db: None) -> None:
    new = candidate("fresh.test")
    known = candidate("example.test")
    existing = Site.objects.create(domain="example.test", enabled=True, notes="kept")

    response = post_json(staff_client, DECIDE, {"ids": [new.pk, known.pk], "action": "approve"})

    assert response.status_code == 200
    site = Site.objects.get(domain="fresh.test")
    assert response.json() == {
        "action": "approve",
        "status": "approved",
        "candidate_ids": sorted([new.pk, known.pk]),
        "created_site_ids": [site.pk],
        "removed_site_ids": [],
    }
    assert site.enabled is False
    assert site.notes == f"Approved discovery candidate {new.pk}"
    existing.refresh_from_db()
    assert (existing.enabled, existing.notes) == (True, "kept")
    assert set(SourceCandidate.objects.values_list("status", flat=True)) == {"approved"}


def test_bulk_reject_hoster_ignore(staff_client: Client, db: None) -> None:
    rows = [candidate(f"c{index}.test") for index in range(4)]

    reject = post_json(staff_client, DECIDE, {"ids": [rows[0].pk, rows[1].pk], "action": "reject"})
    hoster = post_json(staff_client, DECIDE, {"ids": [rows[2].pk], "action": "hoster"})
    ignore = post_json(staff_client, DECIDE, {"ids": [rows[3].pk], "action": "ignore"})

    assert [r.status_code for r in (reject, hoster, ignore)] == [200, 200, 200]
    assert reject.json()["created_site_ids"] == []
    statuses = dict(SourceCandidate.objects.values_list("domain", "status"))
    assert statuses == {
        "c0.test": "rejected",
        "c1.test": "rejected",
        "c2.test": "hoster",
        "c3.test": "ignored",
    }
    assert SourceCandidate.objects.get(domain="c2.test").kind == "file_hoster"
    assert not Site.objects.exists()


def test_undo_approval_deletes_only_the_untouched_created_site(
    staff_client: Client, db: None
) -> None:
    fresh = candidate("fresh.test")
    known = candidate("known.test")
    kept = Site.objects.create(domain="known.test", enabled=True, notes="kept")
    approve = post_json(staff_client, DECIDE, {"ids": [fresh.pk, known.pk], "action": "approve"})
    created = approve.json()["created_site_ids"]

    reset = post_json(staff_client, DECIDE, {"ids": [fresh.pk, known.pk], "action": "reset"})

    assert reset.status_code == 200
    assert reset.json()["status"] == "proposed"
    assert reset.json()["created_site_ids"] == []
    assert reset.json()["removed_site_ids"] == created
    assert list(Site.objects.values_list("pk", flat=True)) == [kept.pk]
    assert set(SourceCandidate.objects.values_list("status", flat=True)) == {"proposed"}

    post_json(staff_client, DECIDE, {"ids": [fresh.pk], "action": "approve"})
    reject = post_json(staff_client, DECIDE, {"ids": [fresh.pk], "action": "reject"})
    assert reject.json()["removed_site_ids"] != []
    assert not Site.objects.filter(domain="fresh.test").exists()


def test_decide_errors(staff_client: Client, db: None) -> None:
    proposed = candidate("proposed.test")
    enabled = candidate("enabled.test")
    crawled = candidate("crawled.test")
    post_json(staff_client, DECIDE, {"ids": [enabled.pk, crawled.pk], "action": "approve"})
    Site.objects.filter(domain="enabled.test").update(enabled=True)
    CrawlTask.objects.create(
        site=Site.objects.get(domain="crawled.test"), url="https://crawled.test/", kind="listing"
    )

    missing = post_json(staff_client, DECIDE, {"ids": [proposed.pk, 999999], "action": "reject"})
    assert missing.status_code == 404
    conflict = post_json(
        staff_client,
        DECIDE,
        {"ids": [proposed.pk, enabled.pk, crawled.pk], "action": "reset"},
    )
    assert conflict.status_code == 409
    reasons = {row["id"]: row["reason"] for row in conflict.json()["conflicts"]}
    assert reasons == {enabled.pk: "site enabled", crawled.pk: "site has crawl data"}
    assert post_json(staff_client, DECIDE, {"ids": [], "action": "reject"}).status_code == 422
    assert post_json(staff_client, DECIDE, {"ids": [1], "action": "enable"}).status_code == 422
    statuses = dict(SourceCandidate.objects.values_list("domain", "status"))
    assert statuses == {
        "enabled.test": "approved",
        "crawled.test": "approved",
        "proposed.test": "proposed",
    }
    assert Site.objects.count() == 2
    again = post_json(staff_client, DECIDE, {"ids": [enabled.pk], "action": "approve"})
    assert again.status_code == 200


def test_search_calls(staff_client: Client, db: None) -> None:
    for index in range(3):
        SearchCall.objects.create(
            query=f"query {index}",
            mode="live",
            status="ok",
            result_count=10,
            cost_usd=Decimal("0.002"),
        )

    data = staff_client.get(SEARCH_CALLS, {"limit": 2}).json()

    assert data["total_calls"] == 3
    assert data["total_cost_usd"] == pytest.approx(0.006)
    assert data["cost_24h_usd"] == pytest.approx(0.006)
    assert [call["query"] for call in data["calls"]] == ["query 2", "query 1"]
