import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest
from django.utils import timezone

from catalog.models import Creator, RequestCount, Site
from catalog.request_accounting import RequestAccounting
from core.http import PoliteClient
from crawler import services
from crawler import worker as worker_module
from crawler.blocking import AGE_VERIFICATION_PROVIDER, detect_block
from crawler.models import CrawlTask
from crawler.sites.base import TaskSpec
from crawler.tests.helpers import load_fixture
from crawler.worker import CrawlWorker
from index.models import Media, Page

UA = "MarcelineBot/0.1"
ROBOTS_URL = "https://site01.example/robots.txt"
ROBOTS = "User-agent: *\nDisallow: /embed/*\nDisallow: /player/*\n"
SEARCH_URL = "https://site01.example/search/lena-hartwell/"
VIDEO_URL = "https://site01.example/video/2099/lena-hartwell-new-video-part-1/"


class FakeSite:
    def __init__(self, routes: dict[str, tuple[int, str]]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        status, body = self.routes.get(url, (404, "not found"))
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})

    def worker(self) -> CrawlWorker:
        client = PoliteClient(
            UA, max_retries=0, transport=httpx.MockTransport(self.handler), sleep=lambda _: None
        )
        return CrawlWorker(client, UA)


@pytest.fixture
def site01(db: None) -> Site:
    return Site.objects.create(domain="site01.example", crawl_delay_s=0)


def add_task(site: Site, url: str, kind: CrawlTask.Kind = CrawlTask.Kind.LISTING) -> CrawlTask:
    services.enqueue(site, [TaskSpec(url, kind)])
    return CrawlTask.objects.get(url=url)


def test_crawls_search_page_then_video_page(site01: Site) -> None:
    lena = Creator.objects.create(name="Lena Hartwell")
    fake = FakeSite(
        {
            ROBOTS_URL: (200, ROBOTS),
            SEARCH_URL: (200, load_fixture("site01.example", "search.html")),
            VIDEO_URL: (200, load_fixture("site01.example", "video-2.html")),
        }
    )
    search = add_task(site01, SEARCH_URL, CrawlTask.Kind.SEARCH)

    processed = fake.worker().run(limit=2)

    search.refresh_from_db()
    assert processed == 2
    assert search.status == CrawlTask.Status.DONE
    assert search.fetched_at is not None
    assert CrawlTask.objects.filter(kind=CrawlTask.Kind.DETAIL).count() == 15
    page = Page.objects.get(url=VIDEO_URL)
    assert list(page.creators.all()) == [lena]
    assert set(page.media.values_list("kind", flat=True)) == {"poster", "frame"}
    assert fake.requested == [ROBOTS_URL, SEARCH_URL, VIDEO_URL]


def test_records_robots_crawl_delay_on_the_site(site01: Site) -> None:
    fake = FakeSite({ROBOTS_URL: (200, ROBOTS + "Crawl-delay: 7\n")})
    add_task(site01, "https://site01.example/embed/2099")

    fake.worker().run(once=True)

    site01.refresh_from_db()
    assert site01.robots_crawl_delay_s == 7.0


def test_skips_robots_disallowed_url_without_fetching(site01: Site) -> None:
    fake = FakeSite({ROBOTS_URL: (200, ROBOTS)})
    task = add_task(site01, "https://site01.example/embed/2099")

    fake.worker().run(once=True)

    task.refresh_from_db()
    assert task.status == CrawlTask.Status.SKIPPED
    assert "robots.txt" in task.decision_reason
    assert task.fetched_at is None
    assert fake.requested == [ROBOTS_URL]


def test_skips_blocked_pages(site01: Site) -> None:
    blocked = load_fixture("site15.example", "blocked-403-home.html")
    fake = FakeSite({ROBOTS_URL: (200, ROBOTS), SEARCH_URL: (403, blocked)})
    task = add_task(site01, SEARCH_URL)

    fake.worker().run(once=True)

    task.refresh_from_db()
    assert task.status == CrawlTask.Status.SKIPPED
    assert task.decision_reason.startswith("blocked: Cloudflare")


def test_not_found_is_skipped(site01: Site) -> None:
    task = add_task(site01, "https://site01.example/tags/nobody/")

    FakeSite({ROBOTS_URL: (200, ROBOTS)}).worker().run(once=True)

    task.refresh_from_db()
    assert (task.status, task.decision_reason) == (CrawlTask.Status.SKIPPED, "HTTP 404")


def test_server_errors_are_retried_later(site01: Site) -> None:
    fake = FakeSite({ROBOTS_URL: (200, ROBOTS), SEARCH_URL: (503, "down")})
    task = add_task(site01, SEARCH_URL)

    fake.worker().run(once=True)

    task.refresh_from_db()
    assert task.status == CrawlTask.Status.PENDING
    assert task.attempts == 1
    assert task.next_attempt_at is not None
    assert task.last_error == "HTTP 503"


def test_unavailable_robots_txt_postpones_the_task(site01: Site) -> None:
    fake = FakeSite({ROBOTS_URL: (500, "error")})
    task = add_task(site01, SEARCH_URL)

    fake.worker().run(once=True)

    task.refresh_from_db()
    assert task.status == CrawlTask.Status.PENDING
    assert task.fetched_at is None
    assert fake.requested == [ROBOTS_URL]


def test_unavailable_robots_txt_pauses_the_site_without_burning_attempts(site01: Site) -> None:
    # Review M6: an outage used to fail every task after MAX_ATTEMPTS and fetch robots.txt once
    # per task. Now the site is paused, the task keeps its attempts, and nothing else is claimed.
    fake = FakeSite({ROBOTS_URL: (503, "down")})
    first = add_task(site01, SEARCH_URL)
    second = add_task(site01, "https://site01.example/tags/nobody/")

    assert fake.worker().run(once=True) == 1

    site01.refresh_from_db()
    first.refresh_from_db()
    second.refresh_from_db()
    assert site01.consecutive_failures == 1
    assert site01.paused_until is not None and site01.paused_until > timezone.now()
    assert "HTTP 503" in site01.pause_reason
    assert (first.status, first.attempts, first.fetched_at) == (CrawlTask.Status.PENDING, 0, None)
    assert second.attempts == 0
    assert fake.requested == [ROBOTS_URL]

    # The pause is over but the site is still down: one robots.txt request, then twice as long.
    Site.objects.filter(pk=site01.pk).update(paused_until=timezone.now() - timedelta(seconds=1))
    before = timezone.now()
    assert fake.worker().run(once=True) == 1
    site01.refresh_from_db()
    assert site01.consecutive_failures == 2
    assert site01.paused_until is not None
    assert site01.paused_until >= before + 2 * services.RETRY_BASE_DELAY
    assert fake.requested == [ROBOTS_URL, ROBOTS_URL]


def test_site_pause_logs_no_url_but_keeps_it_in_the_database(
    site01: Site, caplog: pytest.LogCaptureFixture
) -> None:
    """`RobotsUnavailableError` quotes the robots.txt origin it could not fetch
    (`robots.txt for https://site01.example: HTTP 503`); `record_site_failure` stores that text in
    `Site.pause_reason` in full but must not hand the scheme-prefixed URL to the logger — once a
    Sentry DSN is set that log line would ship as an event (issue: sanitise exception messages
    centrally, 2026-09-18). The site id and domain stay in the message either way.
    """
    fake = FakeSite({ROBOTS_URL: (503, "down")})
    add_task(site01, SEARCH_URL)

    with caplog.at_level(logging.WARNING):
        fake.worker().run(once=True)

    site01.refresh_from_db()
    assert "https://site01.example" in site01.pause_reason
    assert "https://site01.example" not in caplog.text
    assert "<url>" in caplog.text
    assert "site01.example" in caplog.text
    assert "HTTP 503" in caplog.text


def test_connection_failures_in_a_row_pause_the_site(site01: Site) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == ROBOTS_URL:
            return httpx.Response(200, text=ROBOTS)
        raise httpx.ConnectError("connection refused", request=request)

    client = PoliteClient(
        UA, max_retries=0, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    tasks = [add_task(site01, f"https://site01.example/tags/t{n}/") for n in range(5)]

    processed = CrawlWorker(client, UA).run(once=True)

    site01.refresh_from_db()
    assert processed == services.SITE_PAUSE_AFTER_FAILURES
    assert site01.consecutive_failures == services.SITE_PAUSE_AFTER_FAILURES
    assert site01.paused_until is not None
    attempts = sorted(
        CrawlTask.objects.filter(pk__in=[t.pk for t in tasks]).values_list("attempts", flat=True)
    )
    # Two ordinary retries before the site counted as down; the pausing task was refunded.
    assert attempts == [0, 0, 0, 1, 1]
    assert services.claim_task() is None


def test_a_response_ends_the_failure_streak(site01: Site) -> None:
    Site.objects.filter(pk=site01.pk).update(consecutive_failures=2, pause_reason="old")
    add_task(site01, "https://site01.example/tags/nobody/")

    FakeSite({ROBOTS_URL: (200, ROBOTS)}).worker().run(once=True)

    site01.refresh_from_db()
    assert (site01.consecutive_failures, site01.paused_until, site01.pause_reason) == (0, None, "")


@pytest.mark.parametrize("existing", [False, True])
def test_removal_notice_preserves_existing_data(db: None, existing: bool) -> None:
    from index.models import FrameHash, Media

    site = Site.objects.create(domain="site03.example", crawl_delay_s=0)
    url = "https://site03.example/post/0a1b2c3d4e5f6"
    if existing:
        page = Page.objects.create(
            site=site, url=url, title="Original", tags=["tag"], raw_extraction={"original": True}
        )
        creator = Creator.objects.create(name="Example")
        page.creators.add(creator)
        media = Media.objects.create(
            page=page, kind=Media.Kind.POSTER, url="https://site03.example/poster.jpg", sha256="a" * 64
        )
        frame = FrameHash.objects.create(media=media, pdq=b"a" * 32, quality=90)
        original_seen = page.last_seen
    task = add_task(site, url, CrawlTask.Kind.DETAIL)
    FakeSite(
        {
            "https://site03.example/robots.txt": (200, "User-agent: *\nAllow: /"),
            url: (200, load_fixture("site03.example", "post-not-found.html")),
        }
    ).worker().run(once=True)
    task.refresh_from_db()
    assert (task.status, task.decision_reason) == (CrawlTask.Status.DONE, "removed")
    assert Page.objects.count() == int(existing)
    if existing:
        page.refresh_from_db()
        assert page.availability == Page.Availability.REMOVED
        assert page.availability_checked_at is not None
        assert (page.title, page.tags, page.raw_extraction) == (
            "Original",
            ["tag"],
            {"original": True},
        )
        assert page.last_seen == original_seen
        assert list(page.creators.all()) == [creator]
        media.refresh_from_db()
        assert media.sha256 == "a" * 64
        assert FrameHash.objects.get(pk=frame.pk).pdq == b"a" * 32


@pytest.mark.parametrize("status", [404, 410])
@pytest.mark.parametrize("existing", [False, True])
def test_detail_http_removal(site01: Site, status: int, existing: bool) -> None:
    if existing:
        Page.objects.create(site=site01, url=VIDEO_URL, title="Original")
    task = add_task(site01, VIDEO_URL, CrawlTask.Kind.DETAIL)
    FakeSite({ROBOTS_URL: (200, ROBOTS), VIDEO_URL: (status, "not found")}).worker().run(once=True)
    task.refresh_from_db()
    if existing:
        page = Page.objects.get(url=VIDEO_URL)
        assert page.availability == Page.Availability.REMOVED
        assert page.availability_checked_at is not None
        assert page.title == "Original"
        assert task.status == CrawlTask.Status.DONE
        assert "removed" in task.decision_reason
    else:
        assert not Page.objects.exists()
        assert task.status == CrawlTask.Status.SKIPPED


@pytest.mark.parametrize("availability", [Page.Availability.UNKNOWN, Page.Availability.REMOVED])
def test_normal_detail_becomes_playable(site01: Site, availability: str) -> None:
    page = Page.objects.create(site=site01, url=VIDEO_URL, availability=availability)
    add_task(site01, VIDEO_URL, CrawlTask.Kind.DETAIL)
    FakeSite(
        {ROBOTS_URL: (200, ROBOTS), VIDEO_URL: (200, load_fixture("site01.example", "video-2.html"))}
    ).worker().run(once=True)
    page.refresh_from_db()
    assert page.availability == Page.Availability.PLAYABLE
    assert page.availability_checked_at is not None


def test_worker_recovers_expired_media_links_before_claiming(site01: Site) -> None:
    earlier = timezone.now() - timedelta(hours=2)
    services.enqueue(site01, [TaskSpec(VIDEO_URL, CrawlTask.Kind.DETAIL)])
    CrawlTask.objects.update(status=CrawlTask.Status.DONE, fetched_at=earlier)
    page = Page.objects.create(site=site01, url=VIDEO_URL, title="old")
    Media.objects.create(
        page=page,
        kind=Media.Kind.POSTER,
        url="https://cdn.site01.test/tkn/0/1/vid/tok/1789412400/u/2099/old.jpg",
        status=Media.Status.SKIPPED,
        last_error=f"HTTP 404: {services.LINK_EXPIRED}",
        fetched_at=earlier + timedelta(hours=1),
    )
    fake = FakeSite(
        {ROBOTS_URL: (200, ROBOTS), VIDEO_URL: (200, load_fixture("site01.example", "video-2.html"))}
    )

    assert fake.worker().run(once=True) == 1

    assert fake.requested == [ROBOTS_URL, VIDEO_URL]
    assert page.media.filter(kind=Media.Kind.POSTER, status=Media.Status.PENDING).count() == 1


def test_invalid_task_url_fails_the_task_and_the_worker_goes_on(site01: Site) -> None:
    # Review M5: an href with a newline passed the on-site filter and then raised httpx.InvalidURL
    # out of the worker, leaving the task RUNNING to kill the next worker too.
    bad = CrawlTask.objects.create(
        site=site01, url="https://site01.example/video/a\nb/", kind=CrawlTask.Kind.DETAIL, priority=9
    )
    good = add_task(site01, "https://site01.example/tags/nobody/")
    fake = FakeSite({ROBOTS_URL: (200, ROBOTS)})

    assert fake.worker().run(once=True) == 2

    bad.refresh_from_db()
    good.refresh_from_db()
    assert bad.status == CrawlTask.Status.FAILED
    assert bad.last_error.startswith("invalid URL: InvalidURL")
    assert good.status == CrawlTask.Status.SKIPPED
    assert fake.requested == [ROBOTS_URL, "https://site01.example/tags/nobody/"]


def test_unexpected_error_fails_only_that_task(
    site01: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = add_task(site01, "https://site01.example/tags/a/")
    CrawlTask.objects.filter(pk=first.pk).update(priority=9)
    second = add_task(site01, "https://site01.example/tags/b/")
    calls: list[str] = []

    def explode_once(*args: Any, **kwargs: Any) -> str | None:
        calls.append("x")
        if len(calls) == 1:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "boom")
        return detect_block(*args, **kwargs)

    monkeypatch.setattr(worker_module, "detect_block", explode_once)

    assert FakeSite({ROBOTS_URL: (200, ROBOTS)}).worker().run(once=True) == 2

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == CrawlTask.Status.FAILED
    assert first.last_error.startswith("worker error: UnicodeDecodeError")
    assert first.next_attempt_at is None and first.locked_at is None
    assert second.status == CrawlTask.Status.SKIPPED


def test_parse_failure_logs_task_id_not_url(
    site01: Site, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Once Sentry is wired to Django logging, a log message must never carry a piracy URL.

    The failure is logged with the crawl task id and site domain only; the URL never appears in
    the message or its arguments (it stays reconstructable from our own database).
    """
    fake = FakeSite({ROBOTS_URL: (200, ROBOTS), SEARCH_URL: (200, "<html></html>")})
    task = add_task(site01, SEARCH_URL)

    class BrokenAdapter:
        domain = "site01.example"

        def parse(self, kind: CrawlTask.Kind, url: str, body: str) -> Any:
            raise ValueError("unexpected parser bug")

    monkeypatch.setattr(worker_module, "get_adapter", lambda domain: BrokenAdapter())

    with caplog.at_level(logging.ERROR):
        fake.worker().run(once=True)

    task.refresh_from_db()
    assert task.status == CrawlTask.Status.FAILED
    assert task.last_error.startswith("parse error: ValueError")
    assert SEARCH_URL not in caplog.text
    assert f"crawl task id={task.pk} site=site01.example" in caplog.text


AGEVERIF_PAGE = (
    '<html><head><script src="https://www.agegate.example/checker.js?key=abc"></script></head>'
    "<body>gate</body></html>"
)
SELF_DECLARATION_FORM = """<html><head><title>Adults only</title></head><body>
<div class="age-gate"><h1>Are you 18 or older?</h1>
<form method="post" action="/age/"><button name="confirm" value="yes">Yes, I am 18+</button>
<a href="https://www.google.com/">No, leave</a></form></div></body></html>"""


def test_verification_provider_gate_logs_task_id_not_url(
    site01: Site, caplog: pytest.LogCaptureFixture
) -> None:
    """A page that shows an age-verification provider (never automated) logs no URL."""
    fake = FakeSite({ROBOTS_URL: (200, ROBOTS), SEARCH_URL: (200, AGEVERIF_PAGE)})
    task = add_task(site01, SEARCH_URL)

    with caplog.at_level(logging.WARNING):
        fake.worker().run(once=True)

    task.refresh_from_db()
    assert task.status == CrawlTask.Status.SKIPPED
    assert task.decision_reason.startswith(f"blocked: {AGE_VERIFICATION_PROVIDER}")
    assert SEARCH_URL not in caplog.text
    assert f"crawl task id={task.pk} site=site01.example" in caplog.text


def test_self_declaration_gate_still_shown_logs_task_id_not_url(
    site01: Site, caplog: pytest.LogCaptureFixture
) -> None:
    """A self-declaration gate still shown after the approved cookies logs no URL."""
    Site.objects.filter(pk=site01.pk).update(
        age_confirmation={
            "cookies": {"age_verified": "1"},
            "approved_by": "operator",
            "approved_at": "2026-09-15",
        }
    )
    fake = FakeSite({ROBOTS_URL: (200, ROBOTS), SEARCH_URL: (200, SELF_DECLARATION_FORM)})
    task = add_task(site01, SEARCH_URL)

    with caplog.at_level(logging.WARNING):
        fake.worker().run(once=True)

    task.refresh_from_db()
    assert task.status == CrawlTask.Status.SKIPPED
    assert "gate still shown" in task.decision_reason
    assert SEARCH_URL not in caplog.text
    assert f"crawl task id={task.pk} site=site01.example" in caplog.text


def test_expired_media_recovery_runs_at_most_every_interval(
    site01: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | None] = []

    def recover(site_domain: str | None = None) -> int:
        calls.append(site_domain)
        return 0

    monkeypatch.setattr(services, "recover_expired_media", recover)
    now = [0.0]
    client = PoliteClient(
        UA,
        max_retries=0,
        transport=httpx.MockTransport(FakeSite({ROBOTS_URL: (200, ROBOTS)}).handler),
        sleep=lambda _: None,
    )
    worker = CrawlWorker(client, UA, clock=lambda: now[0])
    for n in range(3):
        add_task(site01, f"https://site01.example/tags/t{n}/")

    worker.run(once=True, site_domain="site01.example")
    worker.run(once=True, site_domain="site01.example")
    assert calls == ["site01.example"]

    now[0] += worker_module.MEDIA_RECOVERY_INTERVAL_S
    worker.run(once=True, site_domain="site01.example")
    assert calls == ["site01.example", "site01.example"]


def accounted_worker(fake: FakeSite) -> tuple[CrawlWorker, PoliteClient]:
    client = PoliteClient(
        UA,
        max_retries=0,
        transport=httpx.MockTransport(fake.handler),
        sleep=lambda _: None,
        observer=RequestAccounting(),
    )
    return CrawlWorker(client, UA), client


def test_robots_and_pages_are_counted_against_the_site(site01: Site) -> None:
    fake = FakeSite({ROBOTS_URL: (200, ROBOTS), SEARCH_URL: (503, "down")})
    add_task(site01, SEARCH_URL)
    worker, client = accounted_worker(fake)

    worker.run(once=True)
    client.close()

    rows = RequestCount.objects.values_list("site_id", "role", "kind", "request_type", "outcome")
    assert sorted(rows) == [
        (site01.pk, "origin", "page", "page", "5xx"),
        (site01.pk, "origin", "robots", "robots", "2xx"),
    ]


def test_a_used_up_page_budget_releases_the_task_untouched_until_it_frees_up(
    site01: Site,
) -> None:
    site01.daily_request_budget = 1
    site01.save()
    fake = FakeSite({ROBOTS_URL: (200, ROBOTS), SEARCH_URL: (200, "<html></html>")})
    task = add_task(site01, SEARCH_URL)
    other = add_task(site01, "https://site01.example/tags/other/")
    worker, client = accounted_worker(fake)

    assert worker.run(once=True) == 1  # robots.txt used the budget; the page was refused
    client.close()

    task.refresh_from_db()
    assert fake.requested == [ROBOTS_URL]
    assert (task.status, task.attempts, task.fetched_at) == (CrawlTask.Status.PENDING, 0, None)
    assert task.next_attempt_at is not None and task.next_attempt_at > timezone.now()
    assert "daily request budget used up: site" in task.last_error
    # The refusal flushed the counters, so the site's other tasks are not claimed either.
    assert services.claim_task() is None
    other.refresh_from_db()
    assert other.status == CrawlTask.Status.PENDING
    assert RequestCount.objects.get(outcome="refused").kind == "page"
