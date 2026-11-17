import json
from typing import Any

from django.test import Client
from django.utils import timezone

from catalog.models import Site
from index.models import Media, Page
from index.services import SAFETY_BLOCK_REASON, SAFETY_REVIEW_REASON


def post_json(client: Client, path: str, data: object) -> Any:
    return client.post(path, json.dumps(data), content_type="application/json")


def flagged(site: Site, slug: str = "flagged") -> Page:
    page = Page.objects.create(
        site=site,
        url=f"https://example.test/{slug}",
        title="ambiguous title",
        tags=["tag one", 3],
        safety_status="review",
        safety_matches=[{"term": "ambiguous", "category": "other", "action": "review"}],
        safety_flagged_at=timezone.now(),
    )
    Media.objects.create(
        page=page,
        kind="poster",
        url=f"https://cdn.example.test/{slug}.jpg",
        status="skipped",
        last_error=SAFETY_REVIEW_REASON,
    )
    return page


def test_queue_is_text_only(staff_client: Client, site: Site) -> None:
    page = flagged(site)
    Page.objects.create(site=site, url="https://example.test/clear", title="clear")

    response = staff_client.get("/api/v1/review/safety")

    assert response.status_code == 200
    assert b"media" not in response.content
    assert b"cdn.example.test" not in response.content
    [item] = response.json()["items"]
    assert set(item) == {
        "id",
        "site",
        "title",
        "url",
        "tags",
        "matches",
        "categories",
        "flagged_at",
    }
    assert item["id"] == page.pk
    assert item["tags"] == ["tag one"]
    assert item["matches"] == [{"term": "ambiguous", "category": "other", "action": "review"}]
    assert item["categories"] == ["other"]
    assert staff_client.get("/api/v1/review/counts").json()["safety"] == 1


def test_clear_releases_media(staff_client: Client, site: Site) -> None:
    page = flagged(site)

    response = post_json(staff_client, "/api/v1/review/safety/clear", {"ids": [page.pk]})

    assert response.status_code == 200
    assert response.json() == {"status": "clear", "page_ids": [page.pk]}
    page.refresh_from_db()
    assert page.safety_status == "clear"
    assert page.safety_reviewed_at is not None
    assert page.media.get().status == "pending"
    again = post_json(staff_client, "/api/v1/review/safety/clear", {"ids": [page.pk]})
    assert again.status_code == 409


def test_block_requires_reason(staff_client: Client, site: Site) -> None:
    page = flagged(site)
    path = "/api/v1/review/safety/block"

    assert post_json(staff_client, path, {"ids": [page.pk], "reason": "   "}).status_code == 422
    assert post_json(staff_client, path, {"ids": [page.pk], "reason": ""}).status_code == 422
    assert post_json(staff_client, path, {"ids": [page.pk]}).status_code == 422
    page.refresh_from_db()
    assert page.safety_status == "review"

    response = post_json(staff_client, path, {"ids": [page.pk], "reason": " clear indicator "})

    assert response.status_code == 200
    page.refresh_from_db()
    assert page.safety_status == "blocked"
    assert page.safety_note == "clear indicator"
    assert page.media.get().status == "skipped"
    other = flagged(site, "second")
    other.media.update(status="pending", last_error="")
    post_json(staff_client, path, {"ids": [other.pk], "reason": "clear indicator"})
    assert other.media.get().last_error == SAFETY_BLOCK_REASON


def test_decisions_only_for_review_pages(staff_client: Client, site: Site) -> None:
    page = flagged(site)
    clear = Page.objects.create(site=site, url="https://example.test/ok", title="ok")

    response = post_json(staff_client, "/api/v1/review/safety/clear", {"ids": [page.pk, clear.pk]})
    assert response.status_code == 409
    page.refresh_from_db()
    assert page.safety_status == "review"
    missing = post_json(staff_client, "/api/v1/review/safety/clear", {"ids": [999999]})
    assert missing.status_code == 404
