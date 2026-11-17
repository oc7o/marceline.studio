import hashlib
import io
import subprocess
import tempfile
from collections.abc import Callable
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.core.management import call_command
from django.utils import timezone
from PIL import Image

from catalog.models import Creator, RequestCount, Site, SiteHost
from catalog.request_accounting import RequestAccounting
from core.http import PoliteClient
from index.models import FrameHash, Media, Page
from matching import media as queue
from matching import media_worker
from matching.frames import hash_poster
from matching.media_hash import HashedMedia, HasherUnavailableError
from matching.media_worker import CLIENT_MAX_BYTES, MediaWorker
from matching.pdq import MATCH_DISTANCE, hamming_distance
from matching.tests.images import sprite_sheet, synthetic_image, to_bytes

UA = "MarcelineBot/0.1"
POSTER_URL = "https://cdn.example.test/poster.png"
PREVIEW_URL = "https://cdn.example.test/preview.mp4"
PHOTO_URL = "https://n1.example.test/content/a/b/performer/1000/performer_0040.png"
Route = tuple[int, bytes, str]


class FakeCdn:
    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.requested: list[str] = []
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    @property
    def media_requested(self) -> list[str]:
        """Requests other than robots.txt (a media host without one allows everything)."""
        return [url for url in self.requested if not url.endswith("/robots.txt")]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        status, body, content_type = self.routes.get(url, (404, b"not found", "text/plain"))
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    def worker(self) -> MediaWorker:
        client = PoliteClient(
            UA,
            max_retries=0,
            max_bytes=CLIENT_MAX_BYTES,
            transport=httpx.MockTransport(self.handler),
            clock=lambda: self.now,
            sleep=self.sleep,
        )
        return MediaWorker(client)


def raise_connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.fixture
def page(make_page: Callable[[str], Page]) -> Page:
    return make_page("video-1")


@pytest.fixture
def poster_png() -> bytes:
    return to_bytes(synthetic_image(1))


@pytest.fixture(scope="module")
def preview_mp4() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "preview.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=2:size=320x240:rate=10",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
            timeout=60,
        )
        return path.read_bytes()


def add_media(page: Page, url: str = POSTER_URL, kind: Media.Kind = Media.Kind.POSTER) -> Media:
    # Eligible: the page was parsed and scored clearly safe (ADR 0018). The gate itself is
    # covered in matching/tests/test_media_gate.py.
    return Media.objects.create(page=page, kind=kind, url=url, eligible_at=timezone.now())


def process_one(fake: FakeCdn) -> Media:
    media = queue.claim_media()
    assert media is not None
    fake.worker().process(media)
    media.refresh_from_db()
    return media


def test_claims_newest_available_media_first(page: Page, site: Site) -> None:
    old = add_media(page, "https://cdn.example.test/old.jpg")
    new = add_media(page, "https://cdn.example.test/new.jpg")
    Media.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=1))
    later = add_media(page, "https://cdn.example.test/later.jpg")
    Media.objects.filter(pk=later.pk).update(next_attempt_at=timezone.now() + timedelta(hours=1))
    done = add_media(page, "https://cdn.example.test/done.jpg")
    Media.objects.filter(pk=done.pk).update(status=Media.Status.DONE)

    first = queue.claim_media()
    second = queue.claim_media()

    assert first is not None and first.pk == new.pk
    assert first.status == Media.Status.RUNNING and first.attempts == 1
    assert second is not None and second.pk == old.pk
    assert queue.claim_media() is None


def test_claims_expiring_links_before_other_media(page: Page, site: Site) -> None:
    tokenized = "https://b1.cdn.site03.example/tkn/0/17/vid/tokenAAAAAAA/1789412400/00001/00002"
    old_expiring = add_media(page, f"{tokenized}/vidthumb.mp4", Media.Kind.PREVIEW)
    Media.objects.filter(pk=old_expiring.pk).update(created_at=timezone.now() - timedelta(hours=1))
    newer = add_media(page, "https://cdn.example.test/new.jpg")
    new_expiring = add_media(page, f"{tokenized}/small.jpg")
    no_timestamp = add_media(page, "https://cdn.example.test/vid/abc/2026/x.jpg")

    claimed = [queue.claim_media() for _ in range(4)]

    assert [media.pk for media in claimed if media] == [
        new_expiring.pk,
        old_expiring.pk,
        no_timestamp.pk,
        newer.pk,
    ]


def test_claims_creator_linked_media_before_older_unlinked(
    make_page: Callable[[str], Page], site: Site
) -> None:
    creator = Creator.objects.create(name="Performer")
    linked_page = make_page("linked")
    linked_page.creators.add(creator)
    unlinked_page = make_page("unlinked")

    # Any performer of the page counts, not just a primary credit; the row is also older than
    # the unlinked one, so this proves the creator link outranks recency, not just wins by luck.
    linked = add_media(linked_page, "https://cdn.example.test/linked.jpg")
    Media.objects.filter(pk=linked.pk).update(created_at=timezone.now() - timedelta(hours=1))
    add_media(unlinked_page, "https://cdn.example.test/unlinked.jpg")

    claimed = queue.claim_media()

    assert claimed is not None and claimed.pk == linked.pk


def test_claims_poster_before_hover_frames(page: Page) -> None:
    # The poster is older than the hover frame, so this proves poster-first outranks recency.
    poster = add_media(page, "https://cdn.example.test/video_main.jpg", Media.Kind.POSTER)
    Media.objects.filter(pk=poster.pk).update(created_at=timezone.now() - timedelta(hours=1))
    add_media(page, "https://cdn.example.test/video_1.jpg", Media.Kind.FRAME)

    claimed = queue.claim_media()

    assert claimed is not None and claimed.pk == poster.pk


def test_claim_skips_disabled_sites_and_filters_by_domain(page: Page, site: Site) -> None:
    add_media(page)
    assert queue.claim_media(site_domain="other.test") is None
    Site.objects.filter(pk=site.pk).update(enabled=False)
    assert queue.claim_media() is None


def test_poster_is_hashed_and_bytes_discarded(page: Page, poster_png: bytes) -> None:
    add_media(page)
    fake = FakeCdn({POSTER_URL: (200, poster_png, "image/png")})

    media = process_one(fake)

    assert media.status == Media.Status.DONE
    assert (media.width, media.height) == (640, 360)
    assert media.sha256 == hashlib.sha256(poster_png).hexdigest()
    assert media.byte_size == len(poster_png)
    assert media.content_type == "image/png"
    assert media.fetched_at is not None and media.locked_at is None
    hashes = list(media.hashes.all())
    assert len(hashes) == 1
    assert len(bytes(hashes[0].pdq)) == 32 and hashes[0].quality >= 50


def test_photo_media_is_hashed_on_the_poster_path(page: Page, poster_png: bytes) -> None:
    """A photo post is the work itself (issue #69), but it decodes like any other still image.

    The result must be identical to `matching.frames.hash_poster` — not the sprite or video path.
    """
    add_media(page, PHOTO_URL, Media.Kind.PHOTO)
    fake = FakeCdn({PHOTO_URL: (200, poster_png, "image/png")})

    media = process_one(fake)

    assert media.status == Media.Status.DONE
    assert (media.width, media.height) == (640, 360)
    stored = [
        (index, bytes(pdq), quality)
        for index, pdq, quality in media.hashes.order_by("frame_index").values_list(
            "frame_index", "pdq", "quality"
        )
    ]
    assert stored == [(f.frame_index, f.hash, f.quality) for f in hash_poster(poster_png)]


def test_rehashing_is_idempotent(page: Page, poster_png: bytes) -> None:
    add_media(page)
    fake = FakeCdn({POSTER_URL: (200, poster_png, "image/png")})
    first = process_one(fake)
    first_hash = bytes(first.hashes.get().pdq)
    Media.objects.filter(pk=first.pk).update(status=Media.Status.PENDING)

    second = process_one(fake)

    assert second.hashes.count() == 1
    assert bytes(second.hashes.get().pdq) == first_hash


def test_black_bar_removal_follows_the_setting(
    page: Page, poster_png: bytes, settings: Any
) -> None:
    """Off by default (production), and on only when the operator says so.

    Not retroactive by construction: the worker hashes what it just downloaded, so only media
    fetched after the switch carries de-barred hashes (docs/matching/black-bars.md).
    """
    source = Image.open(io.BytesIO(poster_png))
    padded = Image.new("RGB", (source.width, source.height + 48), (0, 0, 0))
    padded.paste(source, (0, 24))
    letterboxed = to_bytes(padded)
    fake = FakeCdn({POSTER_URL: (200, letterboxed, "image/png")})
    original = hash_poster(poster_png)[0].hash

    assert settings.MATCHING_CROP_BLACK_BARS is False
    add_media(page)
    plain = process_one(fake)
    plain_hash = bytes(plain.hashes.get().pdq)
    assert hamming_distance(original, plain_hash) > MATCH_DISTANCE

    settings.MATCHING_CROP_BLACK_BARS = True
    Media.objects.filter(pk=plain.pk).update(status=Media.Status.PENDING)
    cropped = process_one(fake)
    cropped_hash = bytes(cropped.hashes.get().pdq)

    assert cropped_hash != plain_hash
    assert hamming_distance(original, cropped_hash) <= MATCH_DISTANCE
    # The stored dimensions are the media's own, not the cropped ones.
    assert (cropped.width, cropped.height) == (padded.width, padded.height)


def test_preview_clip_frames_are_hashed(page: Page, preview_mp4: bytes) -> None:
    add_media(page, PREVIEW_URL, Media.Kind.PREVIEW)
    fake = FakeCdn({PREVIEW_URL: (200, preview_mp4, "video/mp4")})

    media = process_one(fake)

    assert media.status == Media.Status.DONE
    frame_indexes = list(media.hashes.values_list("frame_index", flat=True))
    assert 1 < len(frame_indexes) <= media_worker.PREVIEW_MAX_FRAMES
    assert len(set(frame_indexes)) == len(frame_indexes)


EXPIRING_POSTER_URL = "https://cdn.example.test/tkn/1/2/vid/tok3n/1789412400/u/p/full.jpg"
# site03 listing card poster shape (crawler/tests/fixtures/site03.example/listing.html).
EXPIRING_CARD_URL = (
    "https://b1.cdn.example.test/tkn/0/2/img/tokenAAAAAAAAAAAAAAAAA/1789408800/0000000000001/"
    "0000000000002/0.webp"
)


@pytest.mark.parametrize("url", [EXPIRING_POSTER_URL, EXPIRING_CARD_URL])
@pytest.mark.parametrize("status", [403, 404, 410])
def test_expired_links_are_skipped_without_retry(page: Page, status: int, url: str) -> None:
    add_media(page, url)
    fake = FakeCdn({url: (status, b"gone", "text/html")})

    media = process_one(fake)

    assert media.status == Media.Status.SKIPPED
    # crawler.services.recover_expired_media matches this exact reason.
    assert media.last_error == f"HTTP {status}: link expired or gone"
    assert media.next_attempt_at is None and media.fetched_at is not None
    assert not FrameHash.objects.exists()


@pytest.mark.parametrize("status", [403, 404, 410])
def test_plain_gone_media_fails_without_recovery(page: Page, status: int) -> None:
    # Review H3: a plain poster URL carries no token, so a 404 means gone, not expired. It must not
    # carry the reason that makes the crawler re-crawl the page for a fresh link.
    add_media(page)
    fake = FakeCdn({POSTER_URL: (status, b"gone", "text/html")})

    media = process_one(fake)

    assert media.status == Media.Status.FAILED
    assert media.last_error == f"HTTP {status}: media gone"
    assert not media.last_error.endswith(media_worker.LINK_EXPIRED)
    assert media.next_attempt_at is None and media.fetched_at is not None


@pytest.mark.parametrize("status", [429, 503])
def test_throttling_and_server_errors_retry_with_backoff(page: Page, status: int) -> None:
    add_media(page)
    fake = FakeCdn({POSTER_URL: (status, b"busy", "text/plain")})

    media = process_one(fake)

    assert media.status == Media.Status.PENDING
    assert media.next_attempt_at is not None and media.next_attempt_at > timezone.now()
    assert media.last_error == f"HTTP {status}"


def test_network_errors_retry(page: Page) -> None:
    add_media(page)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return raise_connect_error(request)

    client = PoliteClient(
        UA, max_retries=0, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    media = queue.claim_media()
    assert media is not None

    MediaWorker(client).process(media)
    media.refresh_from_db()

    assert media.status == Media.Status.PENDING
    assert "ConnectError" in media.last_error


def test_gives_up_after_max_attempts(page: Page) -> None:
    add_media(page)
    Media.objects.update(attempts=queue.MAX_ATTEMPTS - 1)
    fake = FakeCdn({POSTER_URL: (503, b"busy", "text/plain")})

    media = process_one(fake)

    assert media.status == Media.Status.FAILED


def test_size_limit_per_kind(
    page: Page, poster_png: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(media_worker.MAX_BYTES, Media.Kind.POSTER, 1000)
    add_media(page)
    fake = FakeCdn({POSTER_URL: (200, poster_png, "image/png")})

    media = process_one(fake)

    assert media.status == Media.Status.SKIPPED
    assert "too large" in media.last_error and "> 1000" in media.last_error
    assert not FrameHash.objects.exists()


def test_cdn_media_honours_robots_crawl_delay(page: Page, site: Site, poster_png: bytes) -> None:
    Site.objects.filter(pk=site.pk).update(crawl_delay_s=1, robots_crawl_delay_s=3)
    first, second = "https://cdn.example.test/1.png", "https://cdn.example.test/2.png"
    add_media(page, first)
    add_media(page, second)
    fake = FakeCdn({url: (200, poster_png, "image/png") for url in (first, second)})

    assert fake.worker().run(limit=2) == 2
    # The host's robots.txt is a request too: both media wait their turn after it.
    assert sorted(fake.media_requested) == [first, second]
    assert fake.sleeps == [3.0, 3.0]


def test_media_on_the_site_host_waits_twice_as_long(
    page: Page, site: Site, poster_png: bytes
) -> None:
    Site.objects.filter(pk=site.pk).update(crawl_delay_s=2)
    first, second = "https://example.test/1.png", "https://example.test/2.png"
    add_media(page, first)
    add_media(page, second)
    fake = FakeCdn({url: (200, poster_png, "image/png") for url in (first, second)})

    assert fake.worker().run(limit=2) == 2
    assert fake.sleeps == [4.0, 4.0]  # after robots.txt, then after the first media


def test_html_block_page_is_not_hashed(page: Page) -> None:
    add_media(page)
    fake = FakeCdn({POSTER_URL: (200, b"<html>Just a moment...</html>", "text/html")})

    media = process_one(fake)

    assert media.status == Media.Status.SKIPPED
    assert media.last_error == "not media: text/html"


def test_undecodable_media_fails_without_retry(page: Page) -> None:
    add_media(page)
    fake = FakeCdn({POSTER_URL: (200, b"\xff\xd8 definitely not a jpeg", "image/jpeg")})

    media = process_one(fake)

    assert media.status == Media.Status.FAILED
    assert media.next_attempt_at is None
    assert "cannot decode image" in media.last_error


def test_a_broken_hasher_retries_instead_of_dropping_the_media(
    page: Page, poster_png: bytes
) -> None:
    """A hash child that timed out, died or was shut down says nothing about the bytes.

    Frame hashes are the product, so a fetched and perfectly decodable media must not be failed
    for good because the hasher was unavailable for a moment (the crawl runtime kills a child on
    its deadline and at shutdown, ADR 0030). `MediaDecodeError` stays final — that one is the
    picture's own fault.
    """
    add_media(page)
    fake = FakeCdn({POSTER_URL: (200, poster_png, "image/png")})

    def broken(
        kind: str,
        data: bytes,
        *,
        crop_bars: bool = False,
        sprite_grid: tuple[int, int] | None = None,
    ) -> HashedMedia:
        raise HasherUnavailableError("hash child died: EOFError")

    media = queue.claim_media()
    assert media is not None
    MediaWorker(
        PoliteClient(
            UA,
            max_retries=0,
            max_bytes=CLIENT_MAX_BYTES,
            transport=httpx.MockTransport(fake.handler),
            clock=lambda: fake.now,
            sleep=fake.sleep,
        ),
        hasher=broken,
    ).process(media)
    media.refresh_from_db()

    assert media.status == Media.Status.PENDING
    assert media.next_attempt_at is not None
    assert media.last_error.startswith("hash unavailable: HasherUnavailableError")
    assert not FrameHash.objects.filter(media=media).exists()


def test_hasher_unavailable_logs_no_url_but_keeps_it_in_the_database(
    page: Page, poster_png: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """A `HasherUnavailableError` (ADR 0030's hash pool) can quote the media it gave up on; the
    stored `last_error` keeps that in full, but the WARNING line must not — once a Sentry DSN is
    set it would ship as an event (issue: sanitise exception messages centrally, 2026-09-18)."""
    add_media(page)
    fake = FakeCdn({POSTER_URL: (200, poster_png, "image/png")})

    def broken(
        kind: str,
        data: bytes,
        *,
        crop_bars: bool = False,
        sprite_grid: tuple[int, int] | None = None,
    ) -> HashedMedia:
        raise HasherUnavailableError(f"hash child died reading {POSTER_URL}")

    media = queue.claim_media()
    assert media is not None
    with caplog.at_level("WARNING"):
        MediaWorker(
            PoliteClient(
                UA,
                max_retries=0,
                max_bytes=CLIENT_MAX_BYTES,
                transport=httpx.MockTransport(fake.handler),
                clock=lambda: fake.now,
                sleep=fake.sleep,
            ),
            hasher=broken,
        ).process(media)
    media.refresh_from_db()

    assert POSTER_URL in media.last_error
    assert POSTER_URL not in caplog.text
    assert "<url>" in caplog.text
    assert f"media {media.pk}" in caplog.text


def test_hashing_failure_logs_media_id_not_url(
    page: Page, poster_png: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """Once Sentry is wired to Django logging, a log message must never carry a piracy URL.

    The failure is logged with the media id and site domain only; the URL never appears in the
    message or its arguments (it stays reconstructable from our own database).
    """
    add_media(page)
    fake = FakeCdn({POSTER_URL: (200, poster_png, "image/png")})

    def broken(
        kind: str,
        data: bytes,
        *,
        crop_bars: bool = False,
        sprite_grid: tuple[int, int] | None = None,
    ) -> HashedMedia:
        raise ValueError("unexpected hashing bug")

    media = queue.claim_media()
    assert media is not None
    with caplog.at_level("ERROR"):
        MediaWorker(
            PoliteClient(
                UA,
                max_retries=0,
                max_bytes=CLIENT_MAX_BYTES,
                transport=httpx.MockTransport(fake.handler),
                clock=lambda: fake.now,
                sleep=fake.sleep,
            ),
            hasher=broken,
        ).process(media)
    media.refresh_from_db()

    assert media.status == Media.Status.FAILED
    assert media.last_error.startswith("hash error: ValueError")
    assert POSTER_URL not in caplog.text
    assert f"media id={media.pk} site=example.test" in caplog.text


SPRITE_URL = "https://cdn.example.test/sprite.jpg"


def test_sprite_with_a_declared_grid_is_hashed_tile_by_tile(page: Page) -> None:
    """An adapter that read the grid off the site itself (issue #14, `MediaSpec.sprite_columns`/
    `sprite_rows`) is believed outright: the worker never has to guess."""
    sheet = to_bytes(sprite_sheet(4, 3, (120, 90)))
    media = Media.objects.create(
        page=page,
        kind=Media.Kind.SPRITE,
        url=SPRITE_URL,
        eligible_at=timezone.now(),
        sprite_columns=4,
        sprite_rows=3,
    )
    fake = FakeCdn({SPRITE_URL: (200, sheet, "image/jpeg")})

    fake.worker().process(media)
    media.refresh_from_db()

    assert media.status == Media.Status.DONE
    assert fake.media_requested == [SPRITE_URL]
    assert media.hashes.count() == 12
    assert list(media.hashes.order_by("frame_index").values_list("frame_index", flat=True)) == list(
        range(12)
    )


def test_sprite_without_a_declared_grid_falls_back_to_geometry(page: Page) -> None:
    """No adapter declared the grid, but the sheet's own geometry is unambiguous
    (`matching.sprite_grid`): the worker still recovers tiles instead of refusing."""
    sheet = to_bytes(sprite_sheet(5, 4, (100, 100)))  # the confident case of test_sprite_grid.py
    add_media(page, SPRITE_URL, Media.Kind.SPRITE)
    fake = FakeCdn({SPRITE_URL: (200, sheet, "image/jpeg")})

    media = process_one(fake)

    assert media.status == Media.Status.DONE
    assert media.hashes.count() == 20


def test_sprite_is_skipped_when_the_grid_is_neither_declared_nor_confident(page: Page) -> None:
    """A real request is made (there is no way to know the grid without the image), but a sheet
    geometry alone cannot confidently read is a deliberate skip, not a failure: the same bytes
    would refuse the same way again, so this must never retry."""
    data = to_bytes(synthetic_image(2))  # a plain image, no real sprite grid
    add_media(page, SPRITE_URL, Media.Kind.SPRITE)
    fake = FakeCdn({SPRITE_URL: (200, data, "image/png")})

    media = process_one(fake)

    assert media.status == Media.Status.SKIPPED
    assert fake.media_requested == [SPRITE_URL]
    assert "sprite grid unknown" in media.last_error
    assert media.hashes.count() == 0


def test_no_media_bytes_are_written_to_disk(
    page: Page,
    poster_png: bytes,
    preview_mp4: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sprite sheet decodes on the same in-memory path as a poster (`matching.pdq.open_image`,
    issue #14) — no tempfile, unlike a preview clip's ffmpeg pass (docs/safety.md)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    add_media(page)
    add_media(page, PREVIEW_URL, Media.Kind.PREVIEW)
    Media.objects.create(
        page=page,
        kind=Media.Kind.SPRITE,
        url=SPRITE_URL,
        eligible_at=timezone.now(),
        sprite_columns=4,
        sprite_rows=3,
    )
    fake = FakeCdn(
        {
            POSTER_URL: (200, poster_png, "image/png"),
            PREVIEW_URL: (200, preview_mp4, "video/mp4"),
            SPRITE_URL: (200, to_bytes(sprite_sheet(4, 3, (120, 90))), "image/jpeg"),
        }
    )

    processed = fake.worker().run(limit=10)

    assert processed == 3
    assert set(Media.objects.values_list("status", flat=True)) == {Media.Status.DONE}
    assert list(tmp_path.iterdir()) == []


def test_recovers_stale_running_media(page: Page) -> None:
    media = add_media(page)
    Media.objects.filter(pk=media.pk).update(
        status=Media.Status.RUNNING, locked_at=timezone.now() - timedelta(hours=1)
    )

    assert queue.recover_stale_media() == 1
    media.refresh_from_db()
    assert media.status == Media.Status.PENDING and media.locked_at is None


def test_stale_media_that_used_its_attempts_fails(page: Page) -> None:
    media = add_media(page)
    Media.objects.filter(pk=media.pk).update(
        status=Media.Status.RUNNING,
        attempts=queue.MAX_ATTEMPTS,
        locked_at=timezone.now() - timedelta(hours=1),
    )

    assert queue.recover_stale_media() == 0
    media.refresh_from_db()
    assert media.status == Media.Status.FAILED and media.last_error.startswith("abandoned")


def test_unexpected_error_fails_the_media_and_the_worker_goes_on(
    page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_media(page)

    def explode(self: MediaWorker, media: Media) -> None:
        raise UnicodeError("idna")

    monkeypatch.setattr(MediaWorker, "process", explode)

    assert FakeCdn({}).worker().run(once=True) == 1
    media = Media.objects.get()
    assert media.status == Media.Status.FAILED
    assert media.last_error == "worker error: UnicodeError: idna"


def test_media_worker_command_refuses_a_second_worker_for_the_same_site(site: Site) -> None:
    from django.core.management.base import CommandError
    from django.db import connections

    from core.locks import MEDIA_WORKER_LOCKS

    other = connections.create_connection("default")
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s, %s)", [MEDIA_WORKER_LOCKS, site.pk])
        with pytest.raises(CommandError, match="already running for example"):
            call_command("media_worker", "--once", stdout=StringIO())
    finally:
        other.close()


def test_media_worker_command_exits_when_queue_is_empty(db: None) -> None:
    out = StringIO()
    call_command("media_worker", "--once", stdout=out)
    assert "processed 0 media" in out.getvalue()


def test_reference_creator_priority(page: Page, site: Site) -> None:
    from catalog.models import Creator
    from matching.models import Reference

    creator = Creator.objects.create(name="Reference creator")
    page.creators.add(creator)
    Reference.objects.create(creator=creator, title="Original", sha256="a" * 64, frame_fps=1)
    Reference.objects.create(creator=creator, title="Second", sha256="b" * 64, frame_fps=1)
    priority = add_media(page)
    other = Page.objects.create(site=site, url="https://example.test/other")
    ordinary = add_media(other, "https://cdn.test/new.jpg")
    expiring = add_media(other, "https://cdn.test/vid/token/1789412400/full.jpg")
    claimed = [queue.claim_media() for _ in range(3)]
    assert [row.pk for row in claimed if row] == [expiring.pk, priority.pk, ordinary.pk]


# --- request accounting and budgets (issue #74) ----------------------------------------------


def accounted_worker(fake: FakeCdn) -> MediaWorker:
    client = PoliteClient(
        UA,
        max_retries=0,
        max_bytes=CLIENT_MAX_BYTES,
        transport=httpx.MockTransport(fake.handler),
        clock=lambda: fake.now,
        sleep=fake.sleep,
        observer=RequestAccounting(),
    )
    return MediaWorker(client)


OTHER_CDN_URL = "https://img.cdn.test/poster.png"  # not a subdomain of the site: a CDN


def test_media_is_counted_as_origin_or_cdn(page: Page, site: Site, poster_png: bytes) -> None:
    origin, subdomain = "https://www.example.test/1.png", POSTER_URL
    add_media(page, origin)
    add_media(page, subdomain, kind=Media.Kind.FRAME)
    add_media(page, OTHER_CDN_URL, kind=Media.Kind.FRAME)
    fake = FakeCdn({url: (200, poster_png, "image/png") for url in (origin, subdomain)})
    fake.routes[OTHER_CDN_URL] = (200, poster_png, "image/png")
    worker = accounted_worker(fake)

    assert worker.run(limit=3) == 3
    worker._client.close()

    assert sorted(RequestCount.objects.values_list("host", "role", "kind", "requests")) == [
        ("cdn.example.test", "origin", "media", 1),  # a subdomain is origin unless reviewed
        ("cdn.example.test", "origin", "robots", 1),  # each media host's own robots.txt, once
        ("img.cdn.test", "cdn", "media", 1),
        ("img.cdn.test", "cdn", "robots", 1),
        ("www.example.test", "origin", "media", 1),
        ("www.example.test", "origin", "robots", 1),
    ]


def test_a_used_up_origin_media_budget_holds_that_hosts_media_but_not_the_cdn(
    page: Page, site: Site, poster_png: bytes
) -> None:
    Site.objects.filter(pk=site.pk).update(daily_media_request_budget=1)
    first, second, third = (f"https://example.test/{n}.png" for n in (1, 2, 3))
    for url in (first, second, third):
        add_media(page, url)
    cdn = add_media(page, OTHER_CDN_URL, kind=Media.Kind.FRAME)  # claimed after the posters
    fake = FakeCdn({url: (200, poster_png, "image/png") for url in (first, second, third)})
    fake.routes[OTHER_CDN_URL] = (200, poster_png, "image/png")
    worker = accounted_worker(fake)

    worker.run(once=True)
    worker._client.close()

    assert OTHER_CDN_URL in fake.requested
    assert len([u for u in fake.media_requested if u.startswith("https://example.test/")]) == 1
    waiting = Media.objects.filter(status=Media.Status.PENDING)
    assert waiting.count() == 2
    assert all(row.next_attempt_at and row.next_attempt_at > timezone.now() for row in waiting)
    refused = waiting.get(last_error__startswith="daily request budget used up")
    assert refused.attempts == 0
    cdn.refresh_from_db()
    assert cdn.status == Media.Status.DONE


def test_a_shared_cdn_host_budget_holds_its_media_on_every_site(
    page: Page, site: Site, poster_png: bytes
) -> None:
    other_site = Site.objects.create(domain="other.test")
    other_page = Page.objects.create(
        site=other_site, url="https://other.test/v", title="v", safety_score=1.0
    )
    # A reviewed CDN subdomain of one site, also serving another site's media.
    # Budget 2: the host's robots.txt (counted on every kind) and one media request.
    SiteHost.objects.create(site=site, host="cdn.example.test", role="cdn", daily_request_budget=2)
    add_media(page, POSTER_URL)
    add_media(page, "https://cdn.example.test/second.png")
    elsewhere = add_media(other_page, "https://cdn.example.test/third.png")
    fake = FakeCdn({})

    worker = accounted_worker(fake)
    worker.run(once=True, site_domain="example.test")
    worker._client.close()

    assert len(fake.media_requested) == 1
    elsewhere.refresh_from_db()
    assert elsewhere.next_attempt_at is not None and elsewhere.next_attempt_at > timezone.now()


# --- holding a host's other media (ADR 0030 slot keys) ----------------------------------------


@pytest.mark.parametrize(
    ("busy_host", "urls"),
    [
        # The crawl runtime's `HostBusyError` names the *slot key* (`core.http.host_key`), which
        # has no `www.`: it must still hold the `www.` spelling of the same server.
        (
            "cdn.example.test",
            ["https://www.cdn.example.test/a.png", "https://cdn.example.test/b.png"],
        ),
        # A budget refusal names the host as the URL spells it; both spellings are one server.
        (
            "www.cdn.example.test",
            ["https://cdn.example.test/a.png", "https://www.cdn.example.test/b.png"],
        ),
    ],
)
def test_deferring_holds_every_spelling_of_the_same_server(
    page: Page, busy_host: str, urls: list[str]
) -> None:
    claimed = add_media(page, urls[0])
    waiting = add_media(page, urls[1], kind=Media.Kind.FRAME)
    other = add_media(page, "https://other.test/c.png", kind=Media.Kind.FRAME)
    until = timezone.now() + timedelta(minutes=5)

    held = queue.defer_media(claimed, "host slot busy", host=busy_host, until=until)

    assert held == 1
    waiting.refresh_from_db()
    other.refresh_from_db()
    assert waiting.next_attempt_at == until
    assert other.next_attempt_at is None


def test_deferring_only_holds_media_a_claim_could_have_taken(
    page: Page, make_page: Callable[[str], Page]
) -> None:
    """The hold exists so the worker does not claim the same host's rows one by one.

    A row no claim would hand out is therefore not worth an UPDATE. That is measurable: on
    2026-09-18 the live index held 85,714 pending media, 83,287 of them on pages deactivated by
    targeted indexing (issue #71), so one busy slot on the biggest CDN rewrote 42,140 rows to
    postpone work that was never going to be claimed — and a slot's `retry_at` is a second away.
    """
    claimed = add_media(page, "https://cdn.example.test/a.png")
    waiting = add_media(page, "https://cdn.example.test/b.png", kind=Media.Kind.FRAME)
    parked = add_media(make_page("inactive"), "https://cdn.example.test/c.png")
    Page.objects.filter(pk=parked.page_id).update(active=False, inactive_reason="no target")
    gated = Media.objects.create(
        page=page, kind=Media.Kind.FRAME, url="https://cdn.example.test/d.png"
    )  # never released: eligible_at is NULL (ADR 0018)
    until = timezone.now() + timedelta(minutes=5)

    held = queue.defer_media(claimed, "host slot busy", host="cdn.example.test", until=until)

    assert held == 1
    waiting.refresh_from_db()
    parked.refresh_from_db()
    gated.refresh_from_db()
    assert waiting.next_attempt_at == until
    assert (parked.next_attempt_at, gated.next_attempt_at) == (None, None)


def test_deferring_does_not_hold_a_look_alike_host(page: Page) -> None:
    claimed = add_media(page, "https://cdn.example.test/a.png")
    lookalike = add_media(page, "https://evil-cdn.example.test/b.png", kind=Media.Kind.FRAME)
    until = timezone.now() + timedelta(minutes=5)

    assert queue.defer_media(claimed, "host slot busy", host="cdn.example.test", until=until) == 0

    lookalike.refresh_from_db()
    assert lookalike.next_attempt_at is None


# --- public addresses only (SSRF) ------------------------------------------------------------


def public_only_worker(
    fake: FakeCdn,
    addresses: dict[str, list[str]],
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> MediaWorker:
    client = PoliteClient(
        UA,
        max_retries=0,
        max_bytes=CLIENT_MAX_BYTES,
        transport=httpx.MockTransport(handler or fake.handler),
        clock=lambda: fake.now,
        sleep=fake.sleep,
        public_only=True,
        resolver=lambda host: addresses.get(host, []),
    )
    return MediaWorker(client)


ADDRESSES = {"cdn.example.test": ["93.184.216.34"], "internal.example.test": ["10.0.0.7"]}


@pytest.mark.parametrize(
    "url",
    [
        "https://internal.example.test/poster.png",
        "http://127.0.0.1:8000/admin/poster.png",
        "http://[::1]/poster.png",
        "http://169.254.169.254/latest/meta-data/poster.png",
    ],
)
def test_media_on_a_non_public_address_is_skipped_without_a_request(page: Page, url: str) -> None:
    add_media(page, url)
    fake = FakeCdn({})
    media = queue.claim_media()
    assert media is not None

    public_only_worker(fake, ADDRESSES).process(media)
    media.refresh_from_db()

    assert fake.requested == []
    assert media.status == Media.Status.SKIPPED
    assert media.last_error == "address not allowed: host resolves to a non-public address"
    assert media.fetched_at is None


@pytest.mark.parametrize(
    "location", ["https://internal.example.test/p.png", "http://127.0.0.1/p.png", "file:///etc"]
)
def test_a_redirect_hop_to_a_non_public_address_is_never_requested(
    page: Page, location: str
) -> None:
    add_media(page)
    fake = FakeCdn({})

    def redirect(request: httpx.Request) -> httpx.Response:
        fake.requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": location})

    media = queue.claim_media()
    assert media is not None

    public_only_worker(fake, ADDRESSES, redirect).process(media)
    media.refresh_from_db()

    assert fake.media_requested == [POSTER_URL]
    assert media.status == Media.Status.SKIPPED
    assert media.last_error.startswith("address not allowed: ")


def test_media_on_a_name_that_does_not_resolve_is_retried(page: Page) -> None:
    add_media(page, "https://gone.example.test/poster.png")
    fake = FakeCdn({})
    media = queue.claim_media()
    assert media is not None

    public_only_worker(fake, ADDRESSES).process(media)
    media.refresh_from_db()

    assert fake.requested == []
    assert media.status == Media.Status.PENDING
    assert media.last_error == "address not allowed: host does not resolve"
    assert media.next_attempt_at is not None and media.next_attempt_at > timezone.now()


def test_public_media_is_still_fetched_by_a_public_only_worker(
    page: Page, poster_png: bytes
) -> None:
    add_media(page)
    fake = FakeCdn({POSTER_URL: (200, poster_png, "image/png")})
    media = queue.claim_media()
    assert media is not None

    public_only_worker(fake, ADDRESSES).process(media)
    media.refresh_from_db()

    assert media.status == Media.Status.DONE
    assert fake.requested == ["https://cdn.example.test/robots.txt", POSTER_URL]


def test_media_worker_command_builds_a_public_only_client(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from matching.management.commands import media_worker as command

    clients: list[PoliteClient] = []

    class RecordingWorker(MediaWorker):
        def __init__(self, client: PoliteClient, **kwargs: Any) -> None:
            clients.append(client)
            super().__init__(client, **kwargs)

    monkeypatch.setattr(command, "MediaWorker", RecordingWorker)
    call_command("media_worker", "--once", stdout=StringIO())

    assert len(clients) == 1 and clients[0].public_only
