"""robots.txt for media hosts (ADR 0007 amendment 2026-09-17), against recorded robots.txt bodies.

`fixtures/robots/<host>.txt` are the hosts' real robots.txt files as fetched on 2026-09-17 (text
only): k.site10.example disallows everything, b1.cdn.site03.example asks for a 10 s Crawl-delay,
fastdelivery.site02.example allows everything.
"""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from django.utils import timezone

from catalog.models import RequestCount, Site
from catalog.request_accounting import RequestAccounting
from core.http import PoliteClient
from index.models import FrameHash, Media, Page
from matching import media as queue
from matching.media_robots import (
    REDIRECT_DISALLOWED,
    ROBOTS_DISALLOWED,
    ROBOTS_UNAVAILABLE,
    MediaRobots,
    MediaRobotsUnavailable,
)
from matching.media_worker import CLIENT_MAX_BYTES, MediaWorker
from matching.tests.images import synthetic_image, to_bytes

UA = "MarcelineBot/0.1"
FIXTURES = Path(__file__).parent / "fixtures" / "robots"
BLOCKING = "k.site10.example"
SLOW = "b1.cdn.site03.example"
OPEN = "fastdelivery.site02.example"


def robots_body(host: str) -> bytes:
    return (FIXTURES / f"{host}.txt").read_bytes()


Handler = Callable[[httpx.Request], httpx.Response]


class Hosts:
    """Media hosts serving their recorded robots.txt and a poster for every other path."""

    def __init__(self, poster: bytes, robots: dict[str, int | bytes] | None = None) -> None:
        self.poster = poster
        # host -> recorded body, or a status code for robots.txt; missing hosts answer 404.
        self.robots: dict[str, int | bytes] = robots or {}
        self.requested: list[str] = []
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            answer = self.robots.get(request.url.host, 404)
            if isinstance(answer, bytes):
                return httpx.Response(200, content=answer, headers={"content-type": "text/plain"})
            return httpx.Response(answer, content=b"<html>error</html>")
        return httpx.Response(200, content=self.poster, headers={"content-type": "image/png"})

    def client(self, handler: Handler | None = None, **kwargs: object) -> PoliteClient:
        return PoliteClient(
            UA,
            max_retries=0,
            max_bytes=CLIENT_MAX_BYTES,
            transport=httpx.MockTransport(handler or self.handler),
            clock=lambda: self.now,
            sleep=self.sleep,
            **kwargs,  # type: ignore[arg-type]
        )

    def worker(self, handler: Handler | None = None, **kwargs: object) -> MediaWorker:
        return MediaWorker(self.client(handler, **kwargs), user_agent=UA)


@pytest.fixture
def page(make_page: Callable[[str], Page]) -> Page:
    return make_page("video-1")


@pytest.fixture
def poster() -> bytes:
    return to_bytes(synthetic_image(1))


def add_media(page: Page, url: str, kind: Media.Kind = Media.Kind.POSTER) -> Media:
    return Media.objects.create(page=page, kind=kind, url=url, eligible_at=timezone.now())


def test_a_disallowed_media_host_blocks_its_media_without_requesting_it(
    page: Page, poster: bytes
) -> None:
    first = add_media(page, f"https://{BLOCKING}/p/1.jpg")
    second = add_media(page, f"https://{BLOCKING}/p/2.jpg", Media.Kind.FRAME)
    hosts = Hosts(poster, {BLOCKING: robots_body(BLOCKING)})

    assert hosts.worker().run(once=True) == 2

    # One robots.txt request, cached for the host; neither media URL is ever requested.
    assert hosts.requested == [f"https://{BLOCKING}/robots.txt"]
    for media in (first, second):
        media.refresh_from_db()
        assert media.status == Media.Status.ROBOTS_BLOCKED
        assert media.last_error == ROBOTS_DISALLOWED
        assert media.fetched_at is None and media.next_attempt_at is None
    assert not FrameHash.objects.exists()
    assert queue.claim_media() is None  # blocked rows are final: never claimed again


def test_hashes_stored_before_the_rule_are_kept_and_their_urls_never_refetched(
    page: Page, poster: bytes
) -> None:
    done = add_media(page, f"https://{BLOCKING}/p/old.jpg")
    Media.objects.filter(pk=done.pk).update(status=Media.Status.DONE, fetched_at=timezone.now())
    FrameHash.objects.create(media=done, pdq=bytes(32), quality=100)
    pending = add_media(page, f"https://{BLOCKING}/p/new.jpg")
    hosts = Hosts(poster, {BLOCKING: robots_body(BLOCKING)})

    assert hosts.worker().run(once=True) == 1

    assert f"https://{BLOCKING}/p/old.jpg" not in hosts.requested
    done.refresh_from_db()
    pending.refresh_from_db()
    assert done.status == Media.Status.DONE and done.hashes.count() == 1
    assert pending.status == Media.Status.ROBOTS_BLOCKED


def test_an_allowing_robots_txt_or_none_at_all_lets_media_through(
    page: Page, poster: bytes
) -> None:
    allowed = add_media(page, f"https://{OPEN}/imgs/56/95/a_main.jpg")
    no_robots = add_media(page, "https://cdn.example.test/poster.png", Media.Kind.FRAME)
    hosts = Hosts(poster, {OPEN: robots_body(OPEN)})  # cdn.example.test answers 404

    assert hosts.worker().run(once=True) == 2

    for media in (allowed, no_robots):
        media.refresh_from_db()
        assert media.status == Media.Status.DONE
    assert sorted(hosts.requested) == [
        "https://cdn.example.test/poster.png",
        "https://cdn.example.test/robots.txt",
        f"https://{OPEN}/imgs/56/95/a_main.jpg",
        f"https://{OPEN}/robots.txt",
    ]


def test_the_media_hosts_crawl_delay_spaces_its_media(
    page: Page, site: Site, poster: bytes
) -> None:
    Site.objects.filter(pk=site.pk).update(crawl_delay_s=1)
    for n in (1, 2):
        add_media(page, f"https://{SLOW}/tkn/{n}.jpg")
    hosts = Hosts(poster, {SLOW: robots_body(SLOW)})

    assert hosts.worker().run(once=True) == 2

    assert hosts.requested[0] == f"https://{SLOW}/robots.txt" and len(hosts.requested) == 3
    # Crawl-delay 10 applies from the robots.txt request on, not the site's 1 s.
    assert hosts.sleeps == [10.0, 10.0]


@pytest.mark.parametrize("answer", [503, 429, "network"])
def test_unreadable_robots_txt_holds_the_hosts_media_without_failing_it(
    page: Page, poster: bytes, answer: int | str
) -> None:
    media = add_media(page, f"https://{SLOW}/tkn/1.jpg")
    other = add_media(page, f"https://{SLOW}/tkn/2.jpg", Media.Kind.FRAME)
    hosts = Hosts(poster, {SLOW: 503 if answer == "network" else int(answer)})

    def handler(request: httpx.Request) -> httpx.Response:
        if answer == "network":
            hosts.requested.append(str(request.url))
            raise httpx.ConnectError("connection refused", request=request)
        return hosts.handler(request)

    claimed = queue.claim_media()
    assert claimed is not None and claimed.pk == media.pk
    before = timezone.now()
    hosts.worker(handler).process(claimed)

    assert hosts.requested == [f"https://{SLOW}/robots.txt"]  # never the media itself
    media.refresh_from_db()
    other.refresh_from_db()
    assert media.status == Media.Status.PENDING
    assert media.attempts == 0  # refunded: waiting on robots.txt is not a failed fetch
    assert media.last_error.startswith(f"{ROBOTS_UNAVAILABLE}: robots.txt for https://{SLOW}")
    assert media.next_attempt_at is not None
    assert before + timedelta(seconds=59) < media.next_attempt_at < before + timedelta(minutes=2)
    assert other.next_attempt_at == media.next_attempt_at  # the whole host waits
    assert queue.claim_media() is None


def test_unreadable_robots_txt_backs_off_per_host_like_a_paused_site(poster: bytes) -> None:
    hosts = Hosts(poster, {SLOW: 503})
    client = hosts.client()
    robots = MediaRobots(client, UA)
    page = Page(site_id=1)
    media = Media(page=page, url=f"https://{SLOW}/tkn/1.jpg")

    waits = []
    for _ in range(3):
        with pytest.raises(MediaRobotsUnavailable) as refused:
            robots.check_for(media, 0.0)(media.url)
        assert refused.value.host == SLOW
        waits.append(refused.value.retry_in)
    hosts.robots[SLOW] = robots_body(SLOW)
    robots.check_for(media, 0.0)(media.url)  # readable again: the streak resets

    assert waits == [timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=4)]
    assert robots.crawl_delay_s(media.url) == 10.0
    assert robots._failures == {}


def test_a_redirect_hop_to_a_disallowed_host_is_never_requested(page: Page, poster: bytes) -> None:
    media = add_media(page, f"https://{OPEN}/imgs/1_main.jpg")
    hosts = Hosts(poster, {OPEN: robots_body(OPEN), BLOCKING: robots_body(BLOCKING)})
    target = f"https://{BLOCKING}/p/1.jpg"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == OPEN and request.url.path != "/robots.txt":
            hosts.requested.append(str(request.url))
            return httpx.Response(302, headers={"location": target})
        return hosts.handler(request)

    assert hosts.worker(handler).run(once=True) == 1

    assert target not in hosts.requested
    assert f"https://{BLOCKING}/robots.txt" in hosts.requested
    media.refresh_from_db()
    assert media.status == Media.Status.ROBOTS_BLOCKED
    assert media.last_error == f"{REDIRECT_DISALLOWED}: {target}"


def test_robots_requests_are_counted_but_never_refused_by_a_budget(
    page: Page, site: Site, poster: bytes
) -> None:
    # The crawler uses up the origin page budget (kinds page + robots) every day; the media
    # worker's one robots.txt per host must not hold the site's media behind it.
    Site.objects.filter(pk=site.pk).update(daily_request_budget=0)
    media = add_media(page, "https://example.test/poster.png")
    hosts = Hosts(poster)
    worker = hosts.worker(observer=RequestAccounting())

    assert worker.run(once=True) == 1
    worker._client.close()

    media.refresh_from_db()
    assert media.status == Media.Status.DONE
    assert sorted(RequestCount.objects.values_list("host", "role", "kind", "outcome")) == [
        ("example.test", "origin", "media", "2xx"),
        ("example.test", "origin", "robots", "4xx"),
    ]
    assert set(RequestCount.objects.values_list("site_id", flat=True)) == {site.pk}
