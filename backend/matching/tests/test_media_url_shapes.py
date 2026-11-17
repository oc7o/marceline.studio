"""The media worker and the dead URL shape breaker (ADR 0041, issue #82).

What the breaker does with an answer is `catalog/tests/test_media_shapes.py`. Here: which answers
the worker hands it, and what a suppressed shape does to a claimed media row.
"""

from collections.abc import Callable
from datetime import timedelta

import httpx
import pytest
from django.utils import timezone
from pytest_django.fixtures import Settings

from catalog import media_shapes
from catalog.models import MediaUrlShape, Site
from core.http import PoliteClient
from index.models import Media, Page
from matching import media as queue
from matching.media_worker import CLIENT_MAX_BYTES, MediaWorker
from matching.tests.images import synthetic_image, to_bytes

pytestmark = pytest.mark.django_db

POSTER_URL = "https://cdn.example.test/contents/videos_screenshots/29000/29060/preview.jpg"
POSTER_SHAPE = "cdn.example.test/contents/videos_screenshots/*/*/preview.jpg"
# A tokenized CDN link (`matching.media.EXPIRING_URL_REGEX`): its 404 is about the token's age.
EXPIRING_URL = "https://cdn.example.test/tkn/1/2/vid/tok3n/1789412400/u/p/full.jpg"


class FakeCdn:
    def __init__(self, status: int, body: bytes = b"gone", content_type: str = "text/html") -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.requested: list[str] = []
        self.now = 0.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(
            self.status, content=self.body, headers={"content-type": self.content_type}
        )

    @property
    def media_requested(self) -> list[str]:
        return [url for url in self.requested if not url.endswith("/robots.txt")]

    def worker(self) -> MediaWorker:
        client = PoliteClient(
            "MarcelineBot/0.1",
            max_retries=0,
            max_bytes=CLIENT_MAX_BYTES,
            transport=httpx.MockTransport(self.handler),
            clock=lambda: self.now,
            sleep=lambda seconds: None,
        )
        return MediaWorker(client)


@pytest.fixture
def page(make_page: Callable[[str], Page]) -> Page:
    return make_page("video-1")


@pytest.fixture
def poster_png() -> bytes:
    return to_bytes(synthetic_image(1))


def add_media(page: Page, url: str = POSTER_URL) -> Media:
    return Media.objects.create(
        page=page, kind=Media.Kind.POSTER, url=url, eligible_at=timezone.now()
    )


def process_one(fake: FakeCdn) -> Media:
    media = queue.claim_media()
    assert media is not None
    fake.worker().process(media)
    media.refresh_from_db()
    return media


def shape(site: Site, name: str = POSTER_SHAPE) -> MediaUrlShape | None:
    return MediaUrlShape.objects.filter(site=site, shape=name).first()


@pytest.mark.parametrize("status", [404, 410])
def test_a_settled_absence_is_counted_against_the_shape(
    page: Page, site: Site, status: int
) -> None:
    add_media(page)

    process_one(FakeCdn(status))

    stored = shape(site)
    assert stored is not None and stored.gone_total == 1 and stored.gone_streak == 1


def test_a_success_is_counted_too(page: Page, site: Site, poster_png: bytes) -> None:
    add_media(page)

    process_one(FakeCdn(200, poster_png, "image/png"))

    stored = shape(site)
    assert stored is not None and stored.ok_total == 1 and stored.gone_total == 0


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_an_answer_that_is_not_an_absence_never_moves_a_shape(
    page: Page, site: Site, status: int
) -> None:
    """403 is an authorization answer, 429 and 5xx mean unwell — none of them says "not here"."""
    add_media(page)

    process_one(FakeCdn(status))

    assert shape(site) is None


def test_an_expired_token_never_moves_a_shape(page: Page, site: Site) -> None:
    add_media(page, EXPIRING_URL)

    media = process_one(FakeCdn(404))

    assert media.last_error.endswith("link expired or gone")
    assert not MediaUrlShape.objects.exists()


def test_a_refusal_before_the_request_never_moves_a_shape(page: Page, site: Site) -> None:
    """The safety gate: nothing was sent, so there is nothing to learn (ADR 0018)."""
    add_media(page)
    claimed = queue.claim_media()
    assert claimed is not None
    # Flagged after the claim, exactly as the gate's own tests do it.
    Page.objects.filter(pk=page.pk).update(safety_status=Page.SafetyStatus.BLOCKED)

    fake = FakeCdn(404)
    fake.worker().process(claimed)

    claimed.refresh_from_db()
    assert claimed.status == Media.Status.SKIPPED and not fake.media_requested
    assert not MediaUrlShape.objects.exists()


def test_a_suppressed_shape_is_postponed_and_never_requested(
    page: Page, site: Site, settings: Settings
) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    for _ in range(int(settings.MEDIA_SHAPE_GONE_STREAK)):
        media_shapes.record(site.pk, POSTER_URL, media_shapes.GONE)
    add_media(page)

    fake = FakeCdn(404)
    media = process_one(fake)

    assert not fake.media_requested
    # Postponed, not skipped and not failed: rehabilitation needs nothing re-queued by hand.
    assert media.status == Media.Status.PENDING
    assert media.next_attempt_at is not None and media.next_attempt_at > timezone.now()
    assert media.attempts == 0  # the attempt is refunded
    assert POSTER_SHAPE in media.last_error and "suppressed" in media.last_error
    assert media.url not in media.last_error


def test_another_shape_on_the_same_site_still_runs(
    page: Page, site: Site, settings: Settings, poster_png: bytes
) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    for _ in range(int(settings.MEDIA_SHAPE_GONE_STREAK)):
        media_shapes.record(site.pk, POSTER_URL, media_shapes.GONE)
    other = "https://cdn.example.test/contents/videos_screenshots/29000/29060/566x302/1.jpg"
    add_media(page, other)

    media = process_one(FakeCdn(200, poster_png, "image/png"))

    assert media.status == Media.Status.DONE


def test_the_probe_reopens_the_shape_and_one_success_closes_it(
    page: Page, site: Site, settings: Settings, poster_png: bytes
) -> None:
    settings.MEDIA_URL_BREAKER_ENABLED = True
    for _ in range(int(settings.MEDIA_SHAPE_GONE_STREAK)):
        media_shapes.record(site.pk, POSTER_URL, media_shapes.GONE)
    add_media(page)
    stored = shape(site)
    assert stored is not None and stored.suppressed_until is not None
    # The site repaired its theme; the backoff window has passed.
    MediaUrlShape.objects.filter(pk=stored.pk).update(
        suppressed_until=timezone.now() - timedelta(minutes=1)
    )

    media = process_one(FakeCdn(200, poster_png, "image/png"))

    assert media.status == Media.Status.DONE
    reopened = shape(site)
    assert reopened is not None and reopened.suppressed_until is None


def test_the_breaker_stays_out_of_the_way_while_it_is_off(page: Page, site: Site) -> None:
    for _ in range(50):
        media_shapes.record(site.pk, POSTER_URL, media_shapes.GONE)
    add_media(page)

    fake = FakeCdn(404)
    media = process_one(fake)

    assert fake.media_requested == [POSTER_URL]
    assert media.status == Media.Status.FAILED
