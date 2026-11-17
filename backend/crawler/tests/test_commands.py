from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections

from catalog.models import Site
from core.locks import CRAWL_WORKER_LOCKS
from crawler.models import CrawlTask


def test_crawl_recheck_filters_and_reuses_tasks(db: None) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from index.models import Page

    site = Site.objects.create(domain="site01.example")
    other = Site.objects.create(domain="site02.example")
    stale = Page.objects.create(
        site=site,
        url="https://site01.example/stale",
        availability_checked_at=timezone.now() - timedelta(hours=25),
    )
    unknown = Page.objects.create(site=site, url="https://site01.example/unknown")
    removed = Page.objects.create(
        site=site, url="https://site01.example/removed", availability=Page.Availability.REMOVED
    )
    Page.objects.create(
        site=site, url="https://site01.example/fresh", availability_checked_at=timezone.now()
    )
    Page.objects.create(site=other, url="https://site02.example/other")
    done = CrawlTask.objects.create(
        site=site,
        url=stale.url,
        kind=CrawlTask.Kind.DETAIL,
        status=CrawlTask.Status.DONE,
        attempts=3,
    )
    out = StringIO()
    call_command("crawl_recheck", site=site.domain, limit=1, stdout=out)
    assert "Enqueued 1 task(s)" in out.getvalue()
    done.refresh_from_db()
    assert done.status == CrawlTask.Status.PENDING
    assert done.attempts == 0
    call_command("crawl_recheck", site=site.domain)
    assert set(CrawlTask.objects.values_list("url", flat=True)) == {stale.url, unknown.url}
    out = StringIO()
    call_command("crawl_recheck", site=site.domain, stdout=out)
    assert "Enqueued 0 task(s)" in out.getvalue()
    call_command("crawl_recheck", site=site.domain, include_removed=True)
    assert CrawlTask.objects.get(url=removed.url).kind == CrawlTask.Kind.DETAIL
    call_command("crawl_recheck", site=site.domain, older_than_hours=0)
    assert CrawlTask.objects.filter(url="https://site01.example/fresh").exists()


def test_crawl_recheck_never_checked_with_priority(db: None) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from crawler.services import RECHECK_PRIORITY
    from index.models import Page

    site = Site.objects.create(domain="site01.example")
    never = Page.objects.create(site=site, url="https://site01.example/never")
    Page.objects.create(
        site=site,
        url="https://site01.example/stale",
        availability_checked_at=timezone.now() - timedelta(days=2),
    )
    out = StringIO()

    call_command(
        "crawl_recheck",
        site=site.domain,
        never_checked=True,
        priority=RECHECK_PRIORITY,
        stdout=out,
    )

    assert "Enqueued 1 task(s)" in out.getvalue()
    task = CrawlTask.objects.get()
    assert (task.url, task.priority) == (never.url, RECHECK_PRIORITY)


def test_crawl_recheck_skips_inactive_pages_unless_asked(db: None) -> None:
    from index.models import Page

    site = Site.objects.create(domain="site01.example")
    active = Page.objects.create(site=site, url="https://site01.example/active")
    inactive = Page.objects.create(
        site=site,
        url="https://site01.example/inactive",
        active=False,
        inactive_reason=Page.InactiveReason.UNTARGETED,
    )

    call_command("crawl_recheck", site=site.domain, stdout=StringIO())
    assert list(CrawlTask.objects.values_list("url", flat=True)) == [active.url]

    call_command("crawl_recheck", site=site.domain, include_inactive=True, stdout=StringIO())
    assert CrawlTask.objects.filter(url=inactive.url).exists()


def test_crawl_recover_media_requeues_the_producing_task(db: None) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from index.models import Media, Page

    site = Site.objects.create(domain="site03.example")
    expired_at = timezone.now() - timedelta(days=10)
    task = CrawlTask.objects.create(
        site=site,
        url="https://site03.example/post/abc",
        kind=CrawlTask.Kind.DETAIL,
        status=CrawlTask.Status.DONE,
        fetched_at=expired_at - timedelta(hours=1),
    )
    page = Page.objects.create(site=site, url=task.url)
    Media.objects.create(
        page=page,
        kind=Media.Kind.POSTER,
        url="https://b1.cdn.site03.example/tkn/0/1/vid/tok/1789412400/u/abc/full.jpg",
        status=Media.Status.SKIPPED,
        last_error="HTTP 404: link expired or gone",
        fetched_at=expired_at,
    )
    out = StringIO()

    call_command("crawl_recover_media", "--site", "site03.example", "--max-age-hours", "24", stdout=out)
    assert "Enqueued 0 task(s)." in out.getvalue()

    call_command("crawl_recover_media", "--site", "site03.example", stdout=out)
    assert "Enqueued 1 task(s)." in out.getvalue()
    task.refresh_from_db()
    assert task.status == CrawlTask.Status.PENDING


def test_crawl_worker_refuses_a_second_worker_for_the_same_site(db: None) -> None:
    # Review M9: one crawler per site, enforced with an advisory lock rather than by convention.
    site = Site.objects.create(domain="site01.example")
    Site.objects.create(domain="site02.example")
    other = connections.create_connection("default")
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s, %s)", [CRAWL_WORKER_LOCKS, site.pk])
        with pytest.raises(CommandError, match=r"already running for site01\.com"):
            call_command("crawl_worker", "--site", "site01.example", "--once", stdout=StringIO())
        out = StringIO()
        call_command("crawl_worker", "--site", "site02.example", "--once", stdout=out)
        assert "processed 0 task(s)" in out.getvalue()
    finally:
        other.close()
