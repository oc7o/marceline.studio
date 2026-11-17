"""The crawler in front of a machine-readable reservation (tripwire T2, issue #105).

Three shapes, three behaviours, and the request counts prove the cost: the reservation is read out
of the robots.txt the worker fetches anyway, so detection never adds a request.
"""

import pytest
from django.utils import timezone
from pytest_django.fixtures import Settings

from catalog import robots_policy
from catalog.models import Site, SiteRobotsPolicy
from crawler import services
from crawler.models import CrawlTask
from crawler.sites.base import TaskSpec
from crawler.tests.test_worker import ROBOTS_URL, SEARCH_URL, FakeSite

PLAIN = "User-agent: *\nDisallow: /embed/*\n"
NAMED_AI_ONLY = PLAIN + "\nUser-agent: GPTBot\nDisallow: /\n\nUser-agent: CCBot\nDisallow: /\n"
NAMES_US = PLAIN + "\nUser-agent: MarcelineBot\nDisallow: /\n"
WILDCARD = "User-agent: *\nDisallow: /\n"


@pytest.fixture
def site01(db: None) -> Site:
    return Site.objects.create(domain="site01.example", crawl_delay_s=0)


def _run(site01: Site, robots: str) -> FakeSite:
    fake = FakeSite({ROBOTS_URL: (200, robots), SEARCH_URL: (200, "<html></html>")})
    services.enqueue(site01, [TaskSpec(SEARCH_URL, CrawlTask.Kind.SEARCH)])
    fake.worker().run(limit=1)
    return fake


def test_a_third_party_reservation_is_recorded_and_the_crawl_goes_on(site01: Site) -> None:
    fake = _run(site01, NAMED_AI_ONLY)

    row = SiteRobotsPolicy.objects.get(site=site01)
    assert row.ai_tokens == ["ccbot", "gptbot"] and row.host == "site01.example"
    site01.refresh_from_db()
    assert site01.paused_until is None
    # The page was fetched, and nothing beyond the robots.txt the worker needed anyway was.
    assert fake.requested == [ROBOTS_URL, SEARCH_URL]


def test_a_reservation_naming_us_pauses_the_site_and_hands_the_task_back(
    site01: Site, settings: Settings
) -> None:
    settings.ROBOTS_RESERVATION_PAUSE_ENABLED = True

    fake = _run(site01, NAMES_US)

    site01.refresh_from_db()
    assert site01.pause_reason.startswith(robots_policy.TDM_PAUSE_REASON)
    assert site01.paused_until is not None and site01.paused_until > timezone.now()
    task = CrawlTask.objects.get(url=SEARCH_URL)
    assert task.status == CrawlTask.Status.PENDING
    assert task.attempts == 0, "the attempt is refunded: the URL is not the problem"
    assert task.fetched_at is None
    assert fake.requested == [ROBOTS_URL], "nothing but robots.txt was requested"


def test_a_successful_fetch_never_lifts_a_reservation_pause(
    site01: Site, settings: Settings
) -> None:
    """An outage pause ends when the site answers. A legal reservation does not."""
    settings.ROBOTS_RESERVATION_PAUSE_ENABLED = True
    _run(site01, NAMES_US)
    site01.refresh_from_db()
    paused_until = site01.paused_until

    services.record_site_reachable(site01)

    site01.refresh_from_db()
    assert site01.paused_until == paused_until
    assert site01.pause_reason.startswith(robots_policy.TDM_PAUSE_REASON)


def test_a_wildcard_reservation_needs_no_pause_because_the_url_is_skipped(site01: Site) -> None:
    """Tripwire T2 case (a): `*` is already obeyed — verified here, nothing new added."""
    fake = _run(site01, WILDCARD)

    site01.refresh_from_db()
    assert site01.paused_until is None
    assert SiteRobotsPolicy.objects.get(site=site01).names_wildcard
    task = CrawlTask.objects.get(url=SEARCH_URL)
    assert task.status == CrawlTask.Status.SKIPPED
    assert task.decision_reason == "disallowed by robots.txt"
    assert fake.requested == [ROBOTS_URL]


def test_a_broken_recorder_never_breaks_a_crawl(
    site01: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("no database")

    monkeypatch.setattr(robots_policy, "record", boom)
    fake = _run(site01, NAMED_AI_ONLY)

    assert fake.requested == [ROBOTS_URL, SEARCH_URL]
    assert CrawlTask.objects.get(url=SEARCH_URL).status == CrawlTask.Status.DONE


def test_the_worker_asks_the_site_for_robots_once_per_run(site01: Site) -> None:
    fake = FakeSite({ROBOTS_URL: (200, NAMED_AI_ONLY), SEARCH_URL: (200, "<html></html>")})
    services.enqueue(site01, [TaskSpec(SEARCH_URL, CrawlTask.Kind.SEARCH)])
    worker = fake.worker()
    worker.run(limit=1)
    services.enqueue(site01, [TaskSpec(SEARCH_URL + "2", CrawlTask.Kind.SEARCH)])
    worker.run(limit=1)

    assert fake.requested.count(ROBOTS_URL) == 1
    assert SiteRobotsPolicy.objects.filter(site=site01).count() == 1
