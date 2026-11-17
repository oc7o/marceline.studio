import logging
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog import request_accounting
from catalog.models import Creator, Site
from crawler import services
from crawler.listing_walk import WalkPlan
from crawler.models import CrawlTask
from crawler.sites.base import SEED_PRIORITY, MediaSpec, PageData, ParseResult, TaskSpec
from index.models import Media, Page, SiteCreatorBreadcrumb

LISTING = CrawlTask.Kind.LISTING
DETAIL = CrawlTask.Kind.DETAIL
SEARCH = CrawlTask.Kind.SEARCH
POSTER = Media.Kind.POSTER
PREVIEW = Media.Kind.PREVIEW
DONE = CrawlTask.Status.DONE
PENDING = CrawlTask.Status.PENDING
GONE = "HTTP 404: link expired or gone"  # matching.media_worker skip reason
# Statements one `recheck_pages` batch may cost; only the order of magnitude matters.
PER_BATCH_QUERY_CEILING = 12
CARD_POSTER = "https://cdn.test/vid/a/1789412400/small.jpg"
CARD_PREVIEW = "https://cdn.test/vid/a/1789412400/vidthumb.mp4"
DETAIL_POSTER = "https://cdn.test/vid/b/1789412400/full.jpg"


@pytest.fixture
def site01(db: None) -> Site:
    return Site.objects.create(domain="site01.example", crawl_delay_s=0)


def make_task(site: Site, path: str, kind: CrawlTask.Kind = LISTING, **fields: Any) -> CrawlTask:
    return CrawlTask.objects.create(
        site=site, url=f"https://{site.domain}{path}", kind=kind, **fields
    )


def test_enqueue_dedupes_and_drops_offsite_urls(site01: Site) -> None:
    specs = [
        TaskSpec("https://site01.example/tags/a/", LISTING),
        TaskSpec("https://site01.example/tags/a/#top", LISTING),
        TaskSpec("https://evil.test/x", LISTING),
        TaskSpec("https://cdn.site01.example/x", DETAIL),
        TaskSpec("ftp://site01.example/x", DETAIL),
    ]

    assert services.enqueue(site01, specs) == 2
    assert services.enqueue(site01, specs) == 0
    assert set(CrawlTask.objects.values_list("url", flat=True)) == {
        "https://site01.example/tags/a/",
        "https://cdn.site01.example/x",
    }


def test_enqueue_stores_media_hints(site01: Site) -> None:
    frame = MediaSpec(Media.Kind.FRAME, "https://site01.example/f.jpg")

    services.enqueue(site01, [TaskSpec("https://site01.example/video/1/x/", DETAIL, 10, (frame,))])

    task = CrawlTask.objects.get()
    assert task.priority == 10
    assert task.context == {"media": [{"kind": "frame", "url": "https://site01.example/f.jpg"}]}


def test_enqueue_refresh_requeues_finished_tasks(site01: Site) -> None:
    done = make_task(site01, "/tags/a/", status=CrawlTask.Status.DONE, attempts=2)

    services.enqueue(site01, [TaskSpec(done.url, LISTING)], refresh=True)

    done.refresh_from_db()
    assert (done.status, done.attempts) == (CrawlTask.Status.PENDING, 0)


def test_enqueue_seeds_sets_the_priority_of_whose_seed_it_is(site01: Site) -> None:
    done = make_task(site01, "/search/a/", kind=SEARCH, status=DONE, priority=SEED_PRIORITY)
    pending = make_task(site01, "/search/b/", kind=SEARCH, priority=5)
    both = "https://site01.example/search/c/"
    specs = [
        TaskSpec(done.url, SEARCH, 15),
        TaskSpec(pending.url, SEARCH, 15),
        TaskSpec(both, SEARCH, 15),
        TaskSpec(both, SEARCH, SEED_PRIORITY),
    ]

    assert services.enqueue_seeds(site01, specs, refresh=True) == 1

    assert dict(CrawlTask.objects.values_list("url", "priority")) == {
        done.url: 15,  # a creator moved from Auftrag to the watchlist
        pending.url: 15,
        both: SEED_PRIORITY,  # the highest claim wins
    }
    assert CrawlTask.objects.get(url=done.url).status == PENDING


def test_claim_orders_by_priority_and_skips_unavailable_tasks(site01: Site) -> None:
    now = timezone.now()
    low = make_task(site01, "/low")
    high = make_task(site01, "/high", priority=10)
    make_task(site01, "/later", priority=100, next_attempt_at=now + timedelta(hours=1))
    disabled = Site.objects.create(domain="off.test", enabled=False)
    make_task(disabled, "/x", priority=1000)

    first = services.claim_task(now=now)
    second = services.claim_task(now=now)

    assert first == high
    assert first is not None
    assert (first.status, first.attempts, first.locked_at) == (CrawlTask.Status.RUNNING, 1, now)
    assert second == low
    assert services.claim_task(now=now) is None


def test_claim_filters_by_site(site01: Site) -> None:
    other = Site.objects.create(domain="other.test")
    make_task(other, "/x", priority=5)
    mine = make_task(site01, "/y")

    assert services.claim_task(site_domain="site01.example") == mine


def test_fail_task_backs_off_then_gives_up(site01: Site) -> None:
    now = timezone.now()
    task = make_task(site01, "/x", attempts=1)

    services.fail_task(task, "HTTP 503", now=now)

    assert task.status == CrawlTask.Status.PENDING
    assert task.next_attempt_at == now + services.RETRY_BASE_DELAY
    task.attempts = services.MAX_ATTEMPTS
    services.fail_task(task, "HTTP 503", now=now)
    assert task.status == CrawlTask.Status.FAILED


def test_recover_stale_tasks(site01: Site) -> None:
    now = timezone.now()
    stale = make_task(
        site01, "/stale", status=CrawlTask.Status.RUNNING, locked_at=now - timedelta(hours=1)
    )
    fresh = make_task(site01, "/fresh", status=CrawlTask.Status.RUNNING, locked_at=now)

    assert services.recover_stale_tasks(now=now) == 1

    stale.refresh_from_db()
    fresh.refresh_from_db()
    assert stale.status == CrawlTask.Status.PENDING
    assert fresh.status == CrawlTask.Status.RUNNING


def test_exhausted_tasks_are_never_claimed_or_requeued(site01: Site) -> None:
    # Review M5: a task that kills its worker is recovered as stale; after MAX_ATTEMPTS it fails.
    now = timezone.now()
    exhausted = make_task(
        site01,
        "/poison",
        status=CrawlTask.Status.RUNNING,
        attempts=services.MAX_ATTEMPTS,
        locked_at=now - timedelta(hours=1),
    )
    retry = make_task(
        site01, "/retry", status=CrawlTask.Status.RUNNING, attempts=1, locked_at=now - timedelta(1)
    )
    make_task(site01, "/pending-exhausted", attempts=services.MAX_ATTEMPTS)

    assert services.recover_stale_tasks(now=now) == 1

    exhausted.refresh_from_db()
    assert exhausted.status == CrawlTask.Status.FAILED
    assert exhausted.last_error.startswith("abandoned")
    assert services.claim_task(now=now) == retry
    assert services.claim_task(now=now) is None


def origin_requests(
    site: Site, when: datetime, count: int, *, role: str = "origin", kind: str = "page"
) -> None:
    """Counters as the request accounting writes them (issue #74)."""
    hour = request_accounting.hour_floor(when)
    host = site.domain if role == "origin" else "cdn.test"
    key = (hour, site.pk, host, role, kind, kind, "2xx")
    request_accounting.upsert_counts({key: [count, 0]})


def test_daily_budget_blocks_site_until_window_passes(site01: Site) -> None:
    now = timezone.now()
    site01.daily_request_budget = 2
    site01.save()
    # Real requests count, not tasks: a robots.txt fetch and a retried page used the budget.
    origin_requests(site01, now, 1, kind="robots")
    origin_requests(site01, now, 1)
    pending = make_task(site01, "/next")

    assert services.claim_task(now=now) is None
    assert services.claim_task(now=now + timedelta(hours=25)) == pending


def test_page_budget_is_counted_per_site_on_origin_page_requests_only(site01: Site) -> None:
    now = timezone.now()
    other = Site.objects.create(domain="other.test", daily_request_budget=2)
    origin_requests(other, now, 2)
    origin_requests(other, now - timedelta(hours=25), 9)
    origin_requests(site01, now, 1)
    origin_requests(site01, now, 99, role="cdn", kind="media")
    origin_requests(site01, now, 99, kind="media")  # the origin media budget's business
    make_task(site01, "/done", status=CrawlTask.Status.DONE, fetched_at=now)

    assert services.over_budget_site_ids(now) == [other.pk]
    assert services.over_budget_site_ids(now, site_domain="site01.example") == []
    Site.objects.filter(pk=other.pk).update(daily_request_budget=3)
    assert services.over_budget_site_ids(now) == []


def test_store_parse_result_is_idempotent(site01: Site) -> None:
    task = make_task(
        site01,
        "/video/1/x/",
        kind=DETAIL,
        context={"media": [{"kind": "frame", "url": "https://site01.example/f/1.jpg"}]},
    )
    lena = Creator.objects.create(name="Lena Hartwell")
    Creator.objects.create(name="Thea Farrow")
    result = ParseResult(
        page=PageData(
            url="https://site01.example/video/1/x/",
            title="Some title",
            tags=("lena hartwell fanhub",),
            duration_s=60,
        ),
        media=(MediaSpec(Media.Kind.POSTER, "https://site01.example/p.jpg"),),
        links=(TaskSpec("https://site01.example/tags/lena-hartwell/", LISTING),),
    )

    page = services.store_parse_result(task, result)
    services.store_parse_result(task, result)

    assert page is not None
    assert Page.objects.count() == 1
    assert set(page.media.values_list("kind", flat=True)) == {"poster", "frame"}
    assert list(page.creators.all()) == [lena]
    assert page.raw_extraction["duration_s"] == 60
    assert CrawlTask.objects.filter(url="https://site01.example/tags/lena-hartwell/").count() == 1
    # Breadcrumb (issue #71): a re-crawl of the same page does not inflate the count.
    crumb = SiteCreatorBreadcrumb.objects.get()
    assert (crumb.site_id, crumb.creator_id, crumb.page_count) == (site01.pk, lena.pk, 1)


def test_store_parse_result_ignores_offsite_canonical(site01: Site) -> None:
    task = make_task(site01, "/video/2/y/", kind=DETAIL)

    page = services.store_parse_result(
        task, ParseResult(page=PageData(url="https://mirror.test/video/2/", title="t"))
    )

    assert page is not None
    assert page.url == task.url


def test_match_creators_normalizes_names() -> None:
    creators = [
        Creator(name="Elena Marsh", aliases=["Eléna_Märsh"]),
        Creator(name="Thea Farrow"),
        Creator(name="Iris Holland", aliases=["irisholland"]),
    ]

    matched = services.match_creators(
        ["ELÉNA MÄRSH & Mira Kovacs", "thea lindqvist", "irisholland leaked"], creators
    )

    assert [creator.name for creator in matched] == ["Elena Marsh", "Iris Holland"]


@pytest.mark.parametrize("status", Media.Status.values)
def test_refetch_token_media(site01: Site, status: str) -> None:
    task = make_task(site01, "/post", kind=DETAIL)
    page = Page.objects.create(site=site01, url=task.url)
    old = Media.objects.create(
        page=page, kind=Media.Kind.POSTER, url="https://cdn.test/vid/old/1/poster", status=status
    )
    result = ParseResult(
        page=PageData(url=task.url, title="Post"),
        media=(
            MediaSpec(Media.Kind.POSTER, "https://cdn.test/vid/new/2/poster"),
            MediaSpec(Media.Kind.FRAME, "https://cdn.test/frame"),
        ),
    )
    services.store_parse_result(task, result)
    services.store_parse_result(task, result)
    # A new URL may be on another host, so a robots-blocked row does not cover its kind either.
    retryable = status in (Media.Status.FAILED, Media.Status.SKIPPED, Media.Status.ROBOTS_BLOCKED)
    assert page.media.filter(kind=Media.Kind.POSTER).count() == (2 if retryable else 1)
    assert page.media.filter(kind=Media.Kind.FRAME).count() == 1
    old.refresh_from_db()
    assert old.status == status


def test_failed_media_keeps_url_dedupe(site01: Site) -> None:
    page = Page.objects.create(site=site01, url="https://site01.example/post")
    Media.objects.create(page=page, kind="poster", url=page.url, status="skipped")
    services._attach_media(
        page, [MediaSpec(Media.Kind.POSTER, page.url + "#fragment")], eligible_at=None
    )
    assert page.media.count() == 1


def test_sprite_grid_declared_by_an_adapter_is_stored_on_the_media_row(site01: Site) -> None:
    """An adapter that read the grid off the site's own markup (issue #14) has it believed
    outright: `_attach_media` carries it straight onto the row the media worker reads."""
    page = Page.objects.create(site=site01, url="https://site01.example/post")
    services._attach_media(
        page,
        [
            MediaSpec(
                Media.Kind.SPRITE, "https://cdn.test/sprite.jpg", sprite_columns=5, sprite_rows=4
            )
        ],
        eligible_at=None,
    )
    sprite = page.media.get()
    assert (sprite.sprite_columns, sprite.sprite_rows) == (5, 4)


def test_sprite_grid_round_trips_through_the_task_context(site01: Site) -> None:
    """Card media (a listing thumbnail) waits in `CrawlTask.context` as plain JSON until the
    linked detail page is parsed; a declared sprite grid must survive that round trip too."""
    services.enqueue(
        site01,
        [
            TaskSpec(
                "https://site01.example/video/1/x/",
                DETAIL,
                10,
                (MediaSpec(Media.Kind.SPRITE, "https://cdn.test/sprite.jpg", 5, 4),),
            )
        ],
    )
    task = CrawlTask.objects.get()
    assert task.context == {
        "media": [
            {
                "kind": "sprite",
                "url": "https://cdn.test/sprite.jpg",
                "sprite_columns": 5,
                "sprite_rows": 4,
            }
        ]
    }

    page = services.store_parse_result(task, ParseResult(page=PageData(url=task.url, title="x")))

    assert page is not None
    sprite = page.media.get()
    assert (sprite.sprite_columns, sprite.sprite_rows) == (5, 4)


@pytest.mark.parametrize("reason", ["HTTP 404", "HTTP 410"])
@pytest.mark.parametrize("refresh", [False, True])
def test_dead_detail_cooldown(site01: Site, reason: str, refresh: bool) -> None:
    now = timezone.now()
    task = make_task(site01, "/dead", kind=DETAIL, attempts=1)
    services.skip_task(task, reason, now=now)
    before = CrawlTask.objects.values().get(pk=task.pk)
    specs = [TaskSpec(task.url + "#fragment", DETAIL)]
    assert services.enqueue(site01, specs, refresh=refresh, now=now) == 0
    assert CrawlTask.objects.values().get(pk=task.pk) == before
    assert CrawlTask.objects.count() == 1
    services.enqueue(site01, specs, refresh=True, now=now + services.DEAD_DETAIL_COOLDOWN)
    task.refresh_from_db()
    assert task.status == CrawlTask.Status.PENDING
    assert task.attempts == 0


@pytest.mark.parametrize("kind,reason", [(LISTING, "HTTP 404"), (DETAIL, "blocked: challenge")])
def test_cooldown_does_not_suppress_other_outcomes(
    site01: Site, kind: CrawlTask.Kind, reason: str
) -> None:
    task = make_task(site01, "/other", kind=kind)
    services.skip_task(task, reason)
    services.enqueue(site01, [TaskSpec(task.url, kind)], refresh=True)
    task.refresh_from_db()
    assert task.status == CrawlTask.Status.PENDING


@pytest.mark.parametrize("reason", ["HTTP 404", "HTTP 410"])
def test_dead_detail_with_page_can_be_rechecked(site01: Site, reason: str) -> None:
    task = make_task(site01, "/indexed", kind=DETAIL)
    services.skip_task(task, reason)
    Page.objects.create(site=site01, url=task.url, availability=Page.Availability.REMOVED)
    assert services.recheck_pages(include_removed=True) == 1
    task.refresh_from_db()
    assert task.status == CrawlTask.Status.PENDING


def expire(page: Page, kind: Media.Kind, url: str, at: datetime) -> Media:
    return Media.objects.create(
        page=page, kind=kind, url=url, status=Media.Status.SKIPPED, last_error=GONE, fetched_at=at
    )


def make_expired_post(site: Site, now: datetime) -> tuple[CrawlTask, CrawlTask, Page]:
    """A search page crawled 14 h ago whose card links had expired when the post was indexed."""
    listing = make_task(
        site,
        "/search/x/",
        kind=SEARCH,
        priority=30,
        status=DONE,
        fetched_at=now - timedelta(hours=14),
    )
    card = [{"kind": "poster", "url": CARD_POSTER}, {"kind": "preview", "url": CARD_PREVIEW}]
    detail = make_task(
        site,
        "/video/1/x/",
        kind=DETAIL,
        status=DONE,
        fetched_at=now - timedelta(hours=1),
        discovered_from=listing,
        context={"media": card},
    )
    page = Page.objects.create(site=site, url=detail.url, title="x")
    for kind, url in ((POSTER, CARD_POSTER), (PREVIEW, CARD_PREVIEW), (POSTER, DETAIL_POSTER)):
        expire(page, kind, url, now - timedelta(minutes=30))
    return listing, detail, page


def test_recover_expired_media_refreshes_the_listing_that_carried_the_links(site01: Site) -> None:
    now = timezone.now()
    listing, detail, page = make_expired_post(site01, now)

    assert services.recover_expired_media(now=now) == 1

    listing.refresh_from_db()
    detail.refresh_from_db()
    assert (listing.status, listing.priority) == (PENDING, 30)
    assert detail.status == DONE
    assert detail.context[services.MEDIA_RECOVERIES_KEY] == 1
    assert len(detail.context["media"]) == 2
    assert services.recover_expired_media(now=now) == 0  # the listing is already queued

    fresh = (
        MediaSpec(POSTER, "https://cdn.test/vid/c/1789416000/small.jpg"),
        MediaSpec(PREVIEW, "https://cdn.test/vid/c/1789416000/vidthumb.mp4"),
    )
    links = (TaskSpec(detail.url, DETAIL, 20, fresh),)
    services.store_parse_result(listing, ParseResult(links=links))
    services.complete_task(listing, now=now)

    pending = page.media.filter(status=Media.Status.PENDING).values_list("url", flat=True)
    assert set(pending) == {spec.url for spec in fresh}
    assert services.recover_expired_media(now=now) == 0


def test_listing_card_media_only_fill_kinds_without_usable_media(site01: Site) -> None:
    listing = make_task(site01, "/search/x/", kind=SEARCH)
    page = Page.objects.create(site=site01, url="https://site01.example/video/1/x/")
    Media.objects.create(page=page, kind=PREVIEW, url="https://cdn.test/old.mp4", status="done")
    expire(page, POSTER, "https://cdn.test/old.jpg", timezone.now())
    card = (
        MediaSpec(POSTER, "https://cdn.test/new.jpg"),
        MediaSpec(PREVIEW, "https://cdn.test/new.mp4"),
    )
    new_post = "https://site01.example/video/2/y/"
    links = (TaskSpec(page.url, DETAIL, 20, card), TaskSpec(new_post, DETAIL, 20, card))

    services.store_parse_result(listing, ParseResult(links=links))

    assert set(page.media.values_list("url", flat=True)) == {
        "https://cdn.test/old.mp4",
        "https://cdn.test/old.jpg",
        "https://cdn.test/new.jpg",
    }
    assert Media.objects.count() == 3  # unindexed posts get card media from their task context
    assert len(CrawlTask.objects.get(url=new_post).context["media"]) == 2


def test_recover_expired_media_skips_usable_kinds_removed_pages_and_old_expiries(
    site01: Site,
) -> None:
    now = timezone.now()
    _, _, page = make_expired_post(site01, now)
    for kind in (POSTER, PREVIEW):
        Media.objects.create(page=page, kind=kind, url=f"https://cdn.test/ok-{kind}", status="done")
    assert services.recover_expired_media(now=now) == 0

    Media.objects.filter(status=Media.Status.DONE).delete()
    Page.objects.filter(pk=page.pk).update(availability=Page.Availability.REMOVED)
    assert services.recover_expired_media(now=now) == 0

    Page.objects.filter(pk=page.pk).update(availability=Page.Availability.PLAYABLE)
    later = now + services.MEDIA_RECOVERY_WINDOW
    assert services.recover_expired_media(site_domain="other.test", now=later, max_age=None) == 0
    assert services.recover_expired_media(now=later) == 0
    assert services.recover_expired_media(now=later, max_age=None) == 1


def test_recover_expired_media_ignores_plain_urls_that_are_gone(site01: Site) -> None:
    # Review H3: only tokenized links expire. A plain poster skipped with the old catch-all reason
    # is gone for good, and its page must not be re-crawled for it.
    now = timezone.now()
    listing, detail, page = make_expired_post(site01, now)
    Media.objects.filter(page=page).delete()
    expire(page, POSTER, "https://cdn.test/contents/videos_screenshots/1/full.jpg", now)

    assert services.recover_expired_media(now=now) == 0
    listing.refresh_from_db()
    detail.refresh_from_db()
    assert (listing.status, detail.status) == (DONE, DONE)


def test_recover_expired_media_covers_site03_img_card_posters(site01: Site) -> None:
    # site03 listing cards carry `/tkn/{x}/{y}/img/{token}/{ts}/…/0.webp` posters
    # (fixtures/site03.example/listing.html): tokenized like `/vid/`, so they are recovered too.
    now = timezone.now()
    listing, _, page = make_expired_post(site01, now)
    Media.objects.filter(page=page).delete()
    card = (
        "https://b1.cdn.site03.example/tkn/0/2/img/tok0AbCdEfG/1789408800/0a1b2c3/4d5e6f7/0.webp"
    )
    expire(page, POSTER, card, now)

    assert services.recover_expired_media(now=now) == 1
    listing.refresh_from_db()
    assert listing.status == PENDING


def test_recover_expired_media_caps_tasks_per_run(
    site01: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = timezone.now()
    for n in range(3):
        detail = make_task(
            site01, f"/video/{n}/x/", kind=DETAIL, status=DONE, fetched_at=now - timedelta(hours=2)
        )
        page = Page.objects.create(site=site01, url=detail.url)
        expire(page, POSTER, f"https://cdn.test/vid/t{n}/1789412400/full.jpg", now)
    monkeypatch.setattr(services, "MAX_RECOVERY_TASKS_PER_RUN", 2)

    assert services.recover_expired_media(now=now) == 2
    assert services.recover_expired_media(now=now) == 1


def test_recover_expired_media_falls_back_to_the_detail_page(site01: Site) -> None:
    now = timezone.now()
    listing, detail, _ = make_expired_post(site01, now)
    CrawlTask.objects.filter(pk=listing.pk).update(fetched_at=now)  # re-crawled, post not on it

    assert services.recover_expired_media(now=now) == 1

    listing.refresh_from_db()
    detail.refresh_from_db()
    assert (listing.status, detail.status) == (DONE, PENDING)


def test_recover_expired_media_is_capped_per_page(site01: Site) -> None:
    now = timezone.now()
    listing = make_task(
        site01, "/search/x/", kind=SEARCH, status=DONE, fetched_at=now - timedelta(hours=14)
    )
    detail = make_task(
        site01,
        "/video/1/x/",
        kind=DETAIL,
        status=DONE,
        fetched_at=now - timedelta(hours=13),
        discovered_from=listing,
        context={"media": [{"kind": "preview", "url": CARD_PREVIEW}]},
    )
    page = Page.objects.create(site=site01, url=detail.url)

    requeued = []
    for attempt in range(services.MAX_MEDIA_RECOVERIES + 1):
        expire(page, PREVIEW, f"https://cdn.test/vid/{attempt}/1789412400/vidthumb.mp4", now)
        requeued.append(services.recover_expired_media(now=now))
        now += timedelta(hours=1)  # the listing is crawled again; its fresh link expires again
        CrawlTask.objects.filter(pk=listing.pk).update(status=DONE, fetched_at=now)
        now += timedelta(hours=1)

    assert requeued == [1, 1, 0]
    detail.refresh_from_db()
    assert detail.context[services.MEDIA_RECOVERIES_KEY] == services.MAX_MEDIA_RECOVERIES


@pytest.mark.parametrize("kind", [DETAIL, LISTING, SEARCH])
@pytest.mark.parametrize("tokenized", [True, False])
def test_card_priority_and_claim(site01: Site, kind: CrawlTask.Kind, tokenized: bool) -> None:
    from crawler.sites.base import EXPIRING_DETAIL_PRIORITY, SEED_PRIORITY

    seed = make_task(site01, "/seed", priority=SEED_PRIORITY)
    url = CARD_PREVIEW if tokenized else "https://cdn.test/plain.mp4"
    services.enqueue(
        site01, [TaskSpec("https://site01.example/card", kind, 10, (MediaSpec(PREVIEW, url),))]
    )
    card = CrawlTask.objects.get(url="https://site01.example/card")
    boosted = tokenized and kind == DETAIL
    assert card.priority == (EXPIRING_DETAIL_PRIORITY if boosted else 10)
    assert services.claim_task() == (card if boosted else seed)


@pytest.mark.parametrize("status", [PENDING, DONE, CrawlTask.Status.RUNNING])
def test_pending_card_context_refresh(site01: Site, status: str) -> None:
    old = make_task(site01, "/old")
    fresh = make_task(site01, "/fresh")
    detail = make_task(
        site01,
        "/post",
        DETAIL,
        status=status,
        discovered_from=old,
        context={
            "media": [{"kind": "preview", "url": "https://cdn.test/old"}],
            services.MEDIA_RECOVERIES_KEY: 1,
        },
    )
    original = detail.context.copy()
    services.enqueue(
        site01,
        [TaskSpec(detail.url, DETAIL, media=(MediaSpec(PREVIEW, CARD_PREVIEW),))],
        discovered_from=fresh,
    )
    detail.refresh_from_db()
    if status == PENDING:
        assert detail.context["media"] == [{"kind": "preview", "url": CARD_PREVIEW}]
        assert detail.context[services.MEDIA_RECOVERIES_KEY] == 1
        assert detail.discovered_from == fresh
    else:
        assert detail.context == original
        assert detail.discovered_from == old


def test_expired_context_is_not_stored_and_remains_recoverable(site01: Site) -> None:
    now = timezone.now()
    listing = make_task(site01, "/listing", status=DONE, fetched_at=now - timedelta(hours=2))
    expired = f"https://cdn.test/vid/token/{int(now.timestamp()) - 60}/preview.mp4"
    fresh = f"https://cdn.test/vid/token/{int(now.timestamp()) + 3600}/poster.jpg"
    detail = make_task(
        site01,
        "/post",
        DETAIL,
        status=CrawlTask.Status.RUNNING,
        discovered_from=listing,
        context={
            "media": [
                {"kind": "preview", "url": expired},
                {"kind": "poster", "url": fresh},
                {"kind": "frame", "url": "https://cdn.test/frame.jpg"},
            ]
        },
    )
    page = services.store_parse_result(detail, ParseResult(page=PageData(detail.url, "title")))
    assert page is not None
    assert set(page.media.values_list("url", flat=True)) == {fresh, "https://cdn.test/frame.jpg"}
    services.complete_task(detail)
    assert services.recover_expired_media() == 1
    listing.refresh_from_db()
    assert listing.status == PENDING
    assert services.recover_expired_media() == 0


def test_recheck_page_ids_queues_with_priority_and_refreshes_done(site01: Site) -> None:
    disabled = Site.objects.create(domain="off.test", enabled=False)
    later = timezone.now() + timedelta(hours=1)
    new = Page.objects.create(site=site01, url="https://site01.example/new")
    done = Page.objects.create(site=site01, url="https://site01.example/done")
    waiting = Page.objects.create(site=site01, url="https://site01.example/waiting")
    urgent = Page.objects.create(site=site01, url="https://site01.example/urgent")
    running = Page.objects.create(site=site01, url="https://site01.example/running")
    removed = Page.objects.create(
        site=site01, url="https://site01.example/removed", availability=Page.Availability.REMOVED
    )
    off = Page.objects.create(site=disabled, url="https://off.test/page")
    make_task(site01, "/done", DETAIL, status=DONE, attempts=3, priority=10, last_error="x")
    make_task(site01, "/waiting", DETAIL, attempts=2, priority=5, next_attempt_at=later)
    make_task(site01, "/urgent", DETAIL, priority=services.RECHECK_PRIORITY + 10)
    make_task(site01, "/running", DETAIL, status=CrawlTask.Status.RUNNING, priority=1)

    result = services.recheck_page_ids(
        [new.pk, done.pk, waiting.pk, urgent.pk, running.pk, removed.pk, off.pk, 999999]
    )

    assert result.queued == [new.pk, done.pk, waiting.pk, urgent.pk, removed.pk]
    assert result.in_progress == [running.pk]
    assert result.skipped == [off.pk]
    tasks = {task.url: task for task in CrawlTask.objects.all()}
    assert services.RECHECK_PRIORITY > SEED_PRIORITY
    for page in (new, done, waiting, removed):
        task = tasks[page.url]
        assert (task.kind, task.status, task.priority) == (
            DETAIL,
            PENDING,
            services.RECHECK_PRIORITY,
        )
    assert tasks[done.url].attempts == 0
    assert tasks[done.url].last_error == ""
    # A pending task keeps its retry schedule.
    assert tasks[waiting.url].attempts == 2
    assert tasks[waiting.url].next_attempt_at == later
    assert tasks[urgent.url].priority == services.RECHECK_PRIORITY + 10
    assert tasks[running.url].status == CrawlTask.Status.RUNNING
    assert off.url not in tasks


def test_recheck_pages_never_checked_with_priority(site01: Site) -> None:
    never = Page.objects.create(site=site01, url="https://site01.example/never")
    stale = Page.objects.create(
        site=site01,
        url="https://site01.example/stale",
        availability_checked_at=timezone.now() - timedelta(days=3),
    )
    make_task(site01, "/never", DETAIL, status=DONE, priority=10)

    assert services.recheck_pages(never_checked=True, priority=services.RECHECK_PRIORITY) == 1

    task = CrawlTask.objects.get(url=never.url)
    assert (task.status, task.priority) == (PENDING, services.RECHECK_PRIORITY)
    assert not CrawlTask.objects.filter(url=stale.url).exists()


def test_mark_removed_finds_the_page_under_its_canonical_url(site01: Site) -> None:
    # Review L10: the page is stored under the canonical URL, the task keeps its own URL.
    task = make_task(site01, "/video/new/7/", kind=DETAIL)
    canonical = "https://site01.example/video/new/7/some-slug/"
    page = services.store_parse_result(task, ParseResult(page=PageData(canonical, "title")))
    assert page is not None and page.url == canonical
    assert task.context[services.PAGE_URL_KEY] == canonical

    assert services.mark_removed(task)

    page.refresh_from_db()
    assert page.availability == Page.Availability.REMOVED
    # A later crawl that stores the page under the task URL itself drops the link again.
    Page.objects.filter(pk=page.pk).update(url=task.url)
    services.store_parse_result(task, ParseResult(page=PageData(task.url, "title")))
    task.refresh_from_db()
    assert services.PAGE_URL_KEY not in task.context


def test_context_expiry_marker_is_cleared_once_handled(site01: Site) -> None:
    now = timezone.now()
    listing = make_task(site01, "/listing", status=DONE, fetched_at=now - timedelta(hours=2))
    expired = f"https://cdn.test/vid/token/{int(now.timestamp()) - 60}/preview.mp4"
    context = {"media": [{"kind": "preview", "url": expired}], "keep": 1}
    recovered = make_task(
        site01, "/post", DETAIL, status=DONE, discovered_from=listing, fetched_at=now
    )
    unindexed = make_task(site01, "/gone", DETAIL, status=DONE, fetched_at=now)
    old = make_task(site01, "/old", DETAIL, status=DONE, fetched_at=now)
    Page.objects.create(site=site01, url=recovered.url, title="t")
    mark = {services.EXPIRED_CONTEXT_KEY: now.isoformat()}
    CrawlTask.objects.filter(pk__in=[recovered.pk, unindexed.pk]).update(
        context={**context, **mark}
    )
    too_old = (now - services.MEDIA_RECOVERY_WINDOW - timedelta(hours=1)).isoformat()
    CrawlTask.objects.filter(pk=old.pk).update(
        context={**context, services.EXPIRED_CONTEXT_KEY: too_old}
    )

    assert services.recover_expired_media(now=now) == 1

    for task in (recovered, unindexed, old):
        task.refresh_from_db()
    assert services.EXPIRED_CONTEXT_KEY not in recovered.context
    assert recovered.context["keep"] == 1 and recovered.context[services.MEDIA_RECOVERIES_KEY] == 1
    assert services.EXPIRED_CONTEXT_KEY not in unindexed.context
    # Outside the worker's window: left for `crawl_recover_media` without --max-age-hours.
    assert old.context[services.EXPIRED_CONTEXT_KEY] == too_old
    assert services.recover_expired_media(now=now) == 0


def test_safety_terms_and_creators_are_cached_briefly(
    site01: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalog.models import SafetyTerm

    loads: list[str] = []
    real = services.load_safety_terms

    def counting() -> list[Any]:
        loads.append("terms")
        return real()

    monkeypatch.setattr(services, "load_safety_terms", counting)
    services.cached_safety_terms()
    services.cached_safety_terms()
    assert loads == ["terms"]

    # A save in this process applies at once ...
    SafetyTerm.objects.create(term="zzqx-test-term", category="other", action="review")
    assert any("zzqx" in str(term) for term in services.cached_safety_terms())
    assert loads == ["terms", "terms"]
    # ... and a change made elsewhere within the TTL.
    now = [0.0]
    monkeypatch.setattr("crawler.services.time.monotonic", lambda: now[0])
    services.clear_reference_caches()
    services.cached_safety_terms()
    now[0] += services.REFERENCE_CACHE_TTL_S
    services.cached_safety_terms()
    assert loads == ["terms", "terms", "terms", "terms"]

    lena = Creator.objects.create(name="Lena Hartwell")
    assert services.cached_creators() == [lena]


def test_recheck_pages_batches_its_writes(site01: Site) -> None:
    """The scheduled recheck is O(batches) queries, not O(pages).

    Measured on the development database (81,398 pages): 400 candidate pages of one site cost
    1,429 ms and 1,257 queries — eight statements and its own COMMIT per page — against 99 ms and
    9 queries batched, for the same 157 queued tasks. The whole `crawl_recheck` scheduler job over
    18 sites took 95 s. Wall time is not testable here; how the query count scales is.
    """
    pages = 2 * services.RECHECK_BATCH + 1
    for index in range(pages):
        Page.objects.create(site=site01, url=f"https://site01.example/p{index}")

    with CaptureQueriesContext(connection) as queries:
        assert services.recheck_pages() == pages

    assert CrawlTask.objects.filter(kind=DETAIL, status=PENDING).count() == pages
    # The work is three batches, and a batch is a handful of statements (its own transaction plus
    # `enqueue`'s). A per-page write would be eight statements and one commit per page.
    batches = 3
    assert len(queries) <= PER_BATCH_QUERY_CEILING * batches
    # And whatever the batch size is, a page must never cost a statement of its own.
    assert len(queries) < pages


def test_recheck_pages_batch_keeps_a_site_to_its_own_tasks(site01: Site) -> None:
    """One batch can hold pages of several sites; each task must still belong to its own."""
    other = Site.objects.create(domain="other.test", crawl_delay_s=0)
    here = Page.objects.create(site=site01, url="https://site01.example/a")
    there = Page.objects.create(site=other, url="https://other.test/b")

    assert services.recheck_pages() == 2

    assert CrawlTask.objects.get(url=here.url).site_id == site01.pk
    assert CrawlTask.objects.get(url=there.url).site_id == other.pk


def test_log_walk_logs_task_id_not_url(site01: Site, caplog: pytest.LogCaptureFixture) -> None:
    """The listing walk's own progress line used to carry the crawled URL; once a Sentry DSN is
    set that WARNING-adjacent line would ship as a breadcrumb (issue: sanitise exception messages
    centrally, 2026-09-18). Task id and site domain replace it, matching commit 9ba5d823."""
    task = make_task(site01, "/newest/?page=7", kind=LISTING)
    plan = WalkPlan(links=(), cards=12, admitted=3, ahead=1)

    with caplog.at_level(logging.INFO):
        services._log_walk(task, plan)

    assert task.url not in caplog.text
    assert f"crawl task id={task.pk} site=site01.example" in caplog.text
    assert "3 of 12 card(s) admitted, 1 page(s) ahead" in caplog.text


def test_recover_expired_media_leaves_a_deactivated_page_alone(site01: Site) -> None:
    """Issue #148: media of an inactive page is never claimed (`matching.media.claimable_media`
    filters `page__active=True`), so recovering its links can only spend page budget — and this was
    the path that re-crawled deactivated pages at all, undoing their scrub."""
    now = timezone.now()
    listing, detail, page = make_expired_post(site01, now)
    Page.objects.filter(pk=page.pk).update(
        active=False, inactive_reason=Page.InactiveReason.UNTARGETED, deactivated_at=now
    )

    assert services.recover_expired_media(now=now) == 0

    listing.refresh_from_db()
    detail.refresh_from_db()
    assert (listing.status, detail.status) == (DONE, DONE)
    assert services.MEDIA_RECOVERIES_KEY not in detail.context

    Page.objects.filter(pk=page.pk).update(active=True, inactive_reason="", deactivated_at=None)

    assert services.recover_expired_media(now=now) == 1
