"""The media deny is a property of the fetch, not of the queue (issue #65, ADR 0018).

Every test here asserts on the *transport*: a page that is blocked, under review, gated by its
score, or never scored must not produce a single HTTP request for any media kind. The worker is
driven directly, bypassing the queue, so these fail even if a future change hands it a row the
queue would never have given it.

`MEDIA_KINDS` must name every member of `Media.Kind`; the coverage test below fails when a kind is
added without it, so this guarantee cannot quietly develop a hole.
"""

from collections.abc import Callable

import httpx
import pytest
from django.utils import timezone

from core.http import PoliteClient
from index.media_fetch import MediaFetchDenied, fetch_media, media_denial
from index.models import FrameHash, Media, Page
from index.services import (
    SAFETY_BLOCK_REASON,
    SAFETY_GATE_REASON,
    SAFETY_REVIEW_REASON,
    gate_page_media,
)
from matching import media as queue
from matching.media_worker import CLIENT_MAX_BYTES, MediaWorker
from matching.pdq import HashedFrame

MEDIA_KINDS = (
    Media.Kind.POSTER,
    Media.Kind.FRAME,
    Media.Kind.SPRITE,
    Media.Kind.PREVIEW,
    Media.Kind.PHOTO,
    Media.Kind.CAPTURE,
)
# Page states that must never cause a media request, as (safety_status, safety_score, reason).
DENIED_PAGES = (
    pytest.param(Page.SafetyStatus.BLOCKED, 1.0, SAFETY_BLOCK_REASON, id="blocked"),
    pytest.param(Page.SafetyStatus.REVIEW, 1.0, SAFETY_REVIEW_REASON, id="review"),
    pytest.param(Page.SafetyStatus.CLEAR, 0.5, SAFETY_GATE_REASON, id="gated-low-score"),
    pytest.param(Page.SafetyStatus.CLEAR, None, SAFETY_GATE_REASON, id="never-scored"),
)


class Recorder:
    """An HTTP transport that fails the test if it is ever asked for anything."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(str(request.url))
        return httpx.Response(200, content=b"x", headers={"content-type": "image/png"})

    def client(self) -> PoliteClient:
        return PoliteClient(
            "MarcelineBot/0.1",
            max_retries=0,
            max_bytes=CLIENT_MAX_BYTES,
            transport=httpx.MockTransport(self.handler),
            sleep=lambda _: None,
        )


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def page(make_page: Callable[[str], Page]) -> Page:
    return make_page("video-1")


def set_state(page: Page, status: str, score: float | None) -> None:
    Page.objects.filter(pk=page.pk).update(safety_status=status, safety_score=score)
    page.refresh_from_db()


def add_media(page: Page, kind: Media.Kind, *, eligible: bool = True) -> Media:
    return Media.objects.create(
        page=page,
        kind=kind,
        url=f"https://cdn.example.test/{kind}.bin",
        eligible_at=timezone.now() if eligible else None,
    )


def test_every_media_kind_is_covered_by_the_deny_tests() -> None:
    """A new kind must not slip past this file: add it to MEDIA_KINDS when you add it to Kind."""
    assert set(MEDIA_KINDS) == set(Media.Kind)


@pytest.mark.parametrize(("status", "score", "reason"), DENIED_PAGES)
@pytest.mark.parametrize("kind", MEDIA_KINDS)
def test_no_media_kind_of_a_denied_page_is_ever_requested(
    page: Page, recorder: Recorder, kind: Media.Kind, status: str, score: float | None, reason: str
) -> None:
    media = add_media(page, kind)
    set_state(page, status, score)
    media.refresh_from_db()

    MediaWorker(recorder.client()).process(media)

    media.refresh_from_db()
    assert recorder.requested == []
    assert media.status == Media.Status.SKIPPED
    # Sprites are skipped for their own reason before the gate; every other kind names the gate.
    if kind != Media.Kind.SPRITE:
        assert media.last_error == reason
    assert not FrameHash.objects.exists()


@pytest.mark.parametrize(("status", "score", "reason"), DENIED_PAGES)
def test_a_page_denied_after_the_claim_still_makes_no_request(
    page: Page, recorder: Recorder, status: str, score: float | None, reason: str
) -> None:
    """The worker holds a stale, clear copy of the page: the guard re-reads the database."""
    add_media(page, Media.Kind.POSTER)
    media = queue.claim_media()
    assert media is not None
    set_state(page, status, score)  # changed behind the worker's back

    MediaWorker(recorder.client()).process(media)

    media.refresh_from_db()
    assert recorder.requested == []
    assert (media.status, media.last_error) == (Media.Status.SKIPPED, reason)


def test_media_of_a_listing_card_is_inert_until_its_page_is_parsed(
    page: Page, recorder: Recorder
) -> None:
    media = add_media(page, Media.Kind.POSTER, eligible=False)

    assert queue.claim_media() is None  # the queue never offers it
    MediaWorker(recorder.client()).process(media)  # and a direct call fetches nothing

    media.refresh_from_db()
    assert recorder.requested == []
    assert (media.status, media.last_error) == (Media.Status.SKIPPED, SAFETY_GATE_REASON)


def test_fetch_media_refuses_without_touching_the_network(page: Page, recorder: Recorder) -> None:
    media = add_media(page, Media.Kind.POSTER)
    set_state(page, Page.SafetyStatus.CLEAR, 0.5)

    with recorder.client() as client, pytest.raises(MediaFetchDenied) as denied:
        fetch_media(client, media, max_bytes=1000)

    assert denied.value.reason == SAFETY_GATE_REASON
    assert recorder.requested == []


def test_fetch_media_allows_the_ordinary_case(page: Page, recorder: Recorder) -> None:
    """The control: without this the tests above would pass for the wrong reason."""
    media = add_media(page, Media.Kind.POSTER)

    with recorder.client() as client:
        response = fetch_media(client, media, max_bytes=1000)

    assert response.status == 200
    assert recorder.requested == [media.url]


def test_a_deleted_media_row_is_denied_rather_than_fetched(page: Page) -> None:
    media = add_media(page, Media.Kind.POSTER)
    media_id = media.pk
    Media.objects.filter(pk=media_id).delete()

    assert media_denial(media_id) == "media row is gone"


def test_gating_a_page_keeps_the_urls_but_removes_eligibility(page: Page) -> None:
    poster = add_media(page, Media.Kind.POSTER)
    preview = add_media(page, Media.Kind.PREVIEW)

    assert gate_page_media([page.pk]) == 2

    for media in (poster, preview):
        media.refresh_from_db()
        assert media.eligible_at is None
        assert (media.status, media.last_error) == (Media.Status.SKIPPED, SAFETY_GATE_REASON)
        assert media.url  # the record itself is kept


def test_hashes_are_not_kept_for_a_page_gated_during_the_fetch(page: Page) -> None:
    add_media(page, Media.Kind.POSTER)
    media = queue.claim_media()
    assert media is not None
    set_state(page, Page.SafetyStatus.CLEAR, 0.5)

    queue.store_hashes(media, [HashedFrame(0, b"\x02" * 32, 90)], sha256="0" * 64)

    media.refresh_from_db()
    assert not FrameHash.objects.exists()
    assert (media.status, media.last_error) == (Media.Status.SKIPPED, SAFETY_GATE_REASON)
