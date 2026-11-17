from collections.abc import Callable

import httpx
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.http import PoliteClient
from index.models import FrameHash, Media, Page
from index.services import SAFETY_BLOCK_REASON, SAFETY_REVIEW_REASON
from matching import media as queue
from matching.media_worker import MediaWorker
from matching.pdq import HashedFrame

POSTER_URL = "https://cdn.example.test/poster.png"


@pytest.fixture
def page(make_page: Callable[[str], Page]) -> Page:
    return make_page("video-1")


def add_poster(page: Page) -> Media:
    return Media.objects.create(
        page=page, kind=Media.Kind.POSTER, url=POSTER_URL, eligible_at=timezone.now()
    )


def flag(page: Page, status: Page.SafetyStatus) -> None:
    Page.objects.filter(pk=page.pk).update(safety_status=status)


@pytest.mark.parametrize("status", [Page.SafetyStatus.REVIEW, Page.SafetyStatus.BLOCKED])
def test_claim_skips_media_of_flagged_pages(page: Page, status: Page.SafetyStatus) -> None:
    add_poster(page)
    flag(page, status)

    assert queue.claim_media() is None

    flag(page, Page.SafetyStatus.CLEAR)
    assert queue.claim_media() is not None


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (Page.SafetyStatus.REVIEW, SAFETY_REVIEW_REASON),
        (Page.SafetyStatus.BLOCKED, SAFETY_BLOCK_REASON),
    ],
)
def test_worker_does_not_fetch_media_of_a_page_flagged_after_claim(
    page: Page, status: Page.SafetyStatus, reason: str
) -> None:
    add_poster(page)
    media = queue.claim_media()
    assert media is not None
    media.page.safety_status = status
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=b"x")

    client = PoliteClient("MarcelineBot/0.1", transport=httpx.MockTransport(handler))
    MediaWorker(client).process(media)

    media.refresh_from_db()
    assert requested == []
    assert (media.status, media.last_error) == (Media.Status.SKIPPED, reason)


def test_hashes_are_not_stored_when_the_page_was_flagged_during_the_fetch(page: Page) -> None:
    add_poster(page)
    media = queue.claim_media()
    assert media is not None
    flag(page, Page.SafetyStatus.BLOCKED)

    queue.store_hashes(media, [HashedFrame(0, b"\x02" * 32, 90)], sha256="0" * 64)

    media.refresh_from_db()
    assert not FrameHash.objects.exists()
    assert (media.status, media.last_error) == (Media.Status.SKIPPED, SAFETY_BLOCK_REASON)


def test_the_gate_check_locks_the_page_until_the_hashes_are_written(page: Page) -> None:
    """A block committing between the gate check and the hash insert must wait for the insert.

    Otherwise its `cancel_page_media` runs first, and the insert then leaves frame hashes and a
    DONE media row on a blocked page. The row lock makes the check and the write one step.
    """
    add_poster(page)
    media = queue.claim_media()
    assert media is not None

    with CaptureQueriesContext(connection) as queries:
        queue.store_hashes(media, [HashedFrame(0, b"\x02" * 32, 90)], sha256="0" * 64)

    page_reads = [q["sql"] for q in queries.captured_queries if 'FROM "index_page"' in q["sql"]]
    assert page_reads, "the gate must re-read the page"
    assert all(" FOR NO KEY UPDATE" in sql or " FOR UPDATE" in sql for sql in page_reads)
    assert FrameHash.objects.filter(media=media).count() == 1
