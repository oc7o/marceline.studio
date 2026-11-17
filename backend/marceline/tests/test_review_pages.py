import json
from typing import Any

from django.test import Client

from catalog.models import Site
from crawler.models import CrawlTask
from crawler.services import RECHECK_PRIORITY
from index.models import Page
from matching.models import Reference, ReferenceLeak

RECHECK = "/api/v1/review/pages/recheck"


def post_json(client: Client, path: str, data: object) -> Any:
    return client.post(path, json.dumps(data), content_type="application/json")


def test_recheck_pages_queues_known_pages(staff_client: Client, db: None) -> None:
    site = Site.objects.create(domain="site01.example")
    disabled = Site.objects.create(domain="off.test", enabled=False)
    a = Page.objects.create(site=site, url="https://site01.example/a")
    b = Page.objects.create(site=site, url="https://site01.example/b")
    off = Page.objects.create(site=disabled, url="https://off.test/c")
    CrawlTask.objects.create(site=site, url=b.url, kind="detail", status="done", priority=10)

    response = post_json(staff_client, RECHECK, {"ids": [a.pk, b.pk, off.pk]})

    assert response.status_code == 200
    assert response.json() == {"queued": [a.pk, b.pk], "in_progress": [], "skipped": [off.pk]}
    tasks = CrawlTask.objects.filter(url__in=(a.url, b.url))
    assert set(tasks.values_list("status", "priority")) == {("pending", RECHECK_PRIORITY)}


def test_recheck_pages_unknown_ids_and_auth(
    staff_client: Client, user_client: Client, db: None
) -> None:
    assert post_json(staff_client, RECHECK, {"ids": [999999]}).status_code == 404
    assert post_json(staff_client, RECHECK, {"ids": []}).status_code == 422
    assert post_json(user_client, RECHECK, {"ids": [1]}).status_code == 403
    assert post_json(Client(), RECHECK, {"ids": [1]}).status_code == 401
    assert not CrawlTask.objects.exists()


def test_reference_items_availability_and_hide_removed(
    staff_client: Client, site: Site, reference: Reference
) -> None:
    online = Page.objects.create(
        site=site, url="https://example.test/online", availability="playable"
    )
    removed = Page.objects.create(
        site=site, url="https://example.test/removed", availability="removed"
    )
    for page in (online, removed):
        ReferenceLeak.objects.create(reference=reference, page=page, score=0.8)
    items = "/api/v1/review/references/items"

    everything = staff_client.get(items).json()["items"]
    visible = staff_client.get(items, {"hide_removed": "true"}).json()["items"]

    assert {item["page"]["availability"] for item in everything} == {"playable", "removed"}
    assert "availability_checked_at" in everything[0]["page"]
    assert [item["page"]["id"] for item in visible] == [online.pk]
