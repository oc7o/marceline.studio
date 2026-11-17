"""Per-site age self-confirmation (ADR 0007 amendment): cookies, skips and probe labels."""

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from catalog.models import Site
from core.http import PoliteClient
from crawler import services
from crawler.discovery.probe import probe
from crawler.models import CrawlTask, SourceCandidate
from crawler.sites.base import TaskSpec
from crawler.tests.helpers import load_fixture
from crawler.tests.test_blocking import AGEVERIF_PAGE, SELF_DECLARATION_FORM, YOTI_PAGE
from crawler.worker import CrawlWorker

UA = "MarcelineBot/0.1"
ROBOTS_URL = "https://site01.example/robots.txt"
VIDEO_URL = "https://site01.example/video/2099/lena-hartwell-new-video-part-1/"
APPROVAL: dict[str, Any] = {
    "cookies": {"age_verified": "1"},
    "approved_by": "operator",
    "approved_at": "2026-09-15",
    "note": "click-through gate",
}


class RecordingSite:
    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = {ROBOTS_URL: "User-agent: *\nAllow: /", **routes}
        self.cookies: dict[str, str | None] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.cookies[url] = request.headers.get("cookie")
        if url not in self.routes:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=self.routes[url], headers={"content-type": "text/html"})

    def worker(self) -> CrawlWorker:
        client = PoliteClient(
            UA, max_retries=0, transport=httpx.MockTransport(self.handler), sleep=lambda _: None
        )
        return CrawlWorker(client, UA)


def crawl(age_confirmation: dict[str, Any], body: str) -> tuple[CrawlTask, RecordingSite]:
    site = Site.objects.create(
        domain="site01.example", crawl_delay_s=0, age_confirmation=age_confirmation
    )
    services.enqueue(site, [TaskSpec(VIDEO_URL, CrawlTask.Kind.DETAIL)])
    fake = RecordingSite({VIDEO_URL: body})
    fake.worker().run(once=True)
    return CrawlTask.objects.get(url=VIDEO_URL), fake


@pytest.mark.django_db
def test_approved_site_gets_its_age_cookie() -> None:
    task, fake = crawl(APPROVAL, load_fixture("site01.example", "video-2.html"))

    assert fake.cookies[VIDEO_URL] == "age_verified=1"
    assert task.status == CrawlTask.Status.DONE


@pytest.mark.django_db
@pytest.mark.parametrize(
    "age_confirmation",
    [{}, {"cookies": {"age_verified": "1"}}, {**APPROVAL, "cookies": {"PHPSESSID": "x"}}],
    ids=["no-approval", "missing-approver", "session-cookie"],
)
def test_no_cookie_without_valid_approval(age_confirmation: dict[str, Any]) -> None:
    task, fake = crawl(age_confirmation, load_fixture("site01.example", "video-2.html"))

    assert fake.cookies[VIDEO_URL] is None
    assert task.status == CrawlTask.Status.DONE


@pytest.mark.django_db
def test_revoked_approval_stops_sending_the_cookie() -> None:
    site = Site.objects.create(domain="site01.example", crawl_delay_s=0, age_confirmation=APPROVAL)
    second = "https://site01.example/video/1/other/"
    services.enqueue(site, [TaskSpec(VIDEO_URL, CrawlTask.Kind.DETAIL)])
    fake = RecordingSite({VIDEO_URL: "<html></html>", second: "<html></html>"})
    worker = fake.worker()
    worker.run(once=True)
    Site.objects.filter(pk=site.pk).update(age_confirmation={})
    services.enqueue(site, [TaskSpec(second, CrawlTask.Kind.DETAIL)])

    worker.run(once=True)

    assert fake.cookies[VIDEO_URL] == "age_verified=1"
    assert fake.cookies[second] is None


@pytest.mark.django_db
@pytest.mark.parametrize("body,provider", [(AGEVERIF_PAGE, "AgeGate"), (YOTI_PAGE, "Yoti")])
def test_provider_page_is_skipped_even_with_approval(body: str, provider: str) -> None:
    with patch("crawler.worker.logger") as logger:
        task, _ = crawl(APPROVAL, body)

    assert task.status == CrawlTask.Status.SKIPPED
    assert task.decision_reason == (
        f"blocked: age_verification_provider: {provider} (always skipped, never automated)"
    )
    logger.warning.assert_called_once()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "age_confirmation,reason",
    [
        ({}, "no operator approval (Site.age_confirmation)"),
        (APPROVAL, "still shown with the approved age confirmation cookies"),
    ],
)
def test_self_declaration_gate_is_skipped(age_confirmation: dict[str, Any], reason: str) -> None:
    task, _ = crawl(age_confirmation, SELF_DECLARATION_FORM)

    assert task.status == CrawlTask.Status.SKIPPED
    assert task.decision_reason.startswith("blocked: age_self_declaration gate")
    assert reason in task.decision_reason


def test_client_scopes_domain_cookies_to_the_site() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[str(request.url)] = request.headers.get("cookie")
        return httpx.Response(200)

    client = PoliteClient(UA, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    client.set_domain_cookies("site.test", {"age_verified": "1"})
    for url in ("https://site.test/", "https://www.site.test/", "https://notsite.test/"):
        client.get(url)
    client.set_domain_cookies("site.test", {})
    client.get("https://site.test/again")

    assert seen == {
        "https://site.test/": "age_verified=1",
        "https://www.site.test/": "age_verified=1",
        "https://notsite.test/": None,
        "https://site.test/again": None,
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body,reason",
    [
        (SELF_DECLARATION_FORM, "age_self_declaration"),
        (AGEVERIF_PAGE, "age_verification_provider: AgeGate"),
        (YOTI_PAGE, "age_verification_provider: Yoti"),
        (load_fixture("site19.example", "search.html"), ""),
    ],
    ids=["self-declaration", "agegate", "yoti", "overlay"],
)
def test_probe_records_age_gate_kind(body: str, reason: str) -> None:
    candidate = SourceCandidate.objects.create(domain="new.test")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text=body)

    with patch("crawler.discovery.probe.PROBE_DELAY_S", 0):
        probe(candidate, transport=httpx.MockTransport(handler))

    candidate.refresh_from_db()
    assert candidate.blocked_reason == reason
    assert ("age_notice" in candidate.evidence) == (reason == "")
