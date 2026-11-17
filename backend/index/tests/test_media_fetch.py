"""The media deny is checked at the moment of the request, not before the wait for it (issue #65).

`fetch_media` hands the deny to `PoliteClient` as its `before_request` hook, which runs after the
per-host wait and before every retry and every redirect hop. A page blocked while a worker slept
for its host's slot, or between a retry and the next attempt, gets no request at all.
"""

from collections.abc import Callable

import httpx
import pytest
from django.utils import timezone

from catalog.models import Site
from core.http import PoliteClient
from index.media_fetch import MediaFetchDenied, fetch_media
from index.models import Media, Page
from index.services import SAFETY_BLOCK_REASON

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def media(site: Site) -> Media:
    page = Page.objects.create(
        site=site,
        url="https://example.test/video/1/",
        title="t",
        safety_status=Page.SafetyStatus.CLEAR,
        safety_score=1.0,
    )
    return Media.objects.create(
        page=page, kind=Media.Kind.POSTER, url="https://cdn.test/p.jpg", eligible_at=timezone.now()
    )


def block(media: Media) -> None:
    Page.objects.filter(pk=media.page_id).update(safety_status=Page.SafetyStatus.BLOCKED)


def client(
    requested: list[str], handler: Handler, sleep: Callable[[float], None], retries: int = 0
) -> PoliteClient:
    def record(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return handler(request)

    return PoliteClient(
        "MarcelineBot/0.1", max_retries=retries, transport=httpx.MockTransport(record), sleep=sleep
    )


def test_a_page_blocked_while_waiting_for_the_host_slot_is_not_requested(media: Media) -> None:
    requested: list[str] = []
    polite = client(requested, lambda request: httpx.Response(200), lambda _: block(media))
    polite.get("https://cdn.test/warmup", min_delay_s=60)  # the host's slot is now taken
    requested.clear()

    with pytest.raises(MediaFetchDenied) as denied:
        fetch_media(polite, media, min_delay_s=60)

    assert denied.value.reason == SAFETY_BLOCK_REASON
    assert requested == []


def test_a_page_blocked_before_a_retry_gets_no_second_attempt(media: Media) -> None:
    requested: list[str] = []
    polite = client(requested, lambda request: httpx.Response(503), lambda _: block(media), 2)

    with pytest.raises(MediaFetchDenied):
        fetch_media(polite, media)

    assert requested == [media.url]


def test_a_page_blocked_before_a_redirect_hop_is_not_followed(media: Media) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        block(media)  # decided while the first response was on its way
        return httpx.Response(302, headers={"location": "https://other-cdn.test/p.jpg"})

    with pytest.raises(MediaFetchDenied):
        fetch_media(client(requested, handler, lambda _: None), media)

    assert requested == [media.url]
