"""Scan once: media already hashed elsewhere is not downloaded or not hashed again (ADR 0015)."""

import hashlib
from collections.abc import Callable

import httpx
import pytest
from django.utils import timezone

from catalog.models import Site
from core.http import PoliteClient
from index.models import FrameHash, Media, Page
from index.services import SAFETY_BLOCK_REASON
from matching import media as queue
from matching import media_worker
from matching.media_worker import CLIENT_MAX_BYTES, MediaWorker
from matching.models import MediaReuse
from matching.services import media_content_key, mirror_key
from matching.tests.images import synthetic_image, to_bytes

SITE02 = "https://fastdelivery.site02.example/imgs/56/95/0a1b2c3d4e5f607_main.jpg"
SITE04 = "https://imgs.cdn.site04.example/fastdelivery/imgs/56/95/0a1b2c3d4e5f607_main.jpg?v=2"
TOKENIZED = "https://b2.cdn.site03.example/tkn/1/15/vid/{token}/{expiry}/0000000000001/0000000000002/full.jpg"
FRAMES = ((0, b"\x01" * 32, 90), (1, b"\x02" * 32, 80))


class Cdn:
    """Mock transport that records every request."""

    def __init__(self, routes: dict[str, tuple[int, bytes, str]] | None = None) -> None:
        self.routes = routes or {}
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(str(request.url))
        status, body, content_type = self.routes.get(
            str(request.url), (404, b"not found", "text/plain")
        )
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    def worker(self) -> MediaWorker:
        client = PoliteClient(
            "MarcelineBot/0.1",
            max_retries=0,
            max_bytes=CLIENT_MAX_BYTES,
            transport=httpx.MockTransport(self.handler),
            sleep=lambda _: None,
        )
        return MediaWorker(client)


@pytest.fixture
def mirror_page(db: None) -> Page:
    mirror = Site.objects.create(domain="mirror.test", name="Mirror")
    return Page.objects.create(site=mirror, url="https://mirror.test/video", title="mirror")


@pytest.fixture
def page(make_page: Callable[[str], Page]) -> Page:
    return make_page("video")


def hashed_media(
    page: Page,
    url: str,
    *,
    kind: Media.Kind = Media.Kind.POSTER,
    sha256: str = "a" * 64,
    frames: tuple[tuple[int, bytes, int], ...] = FRAMES,
) -> Media:
    media = Media.objects.create(
        page=page,
        kind=kind,
        url=url,
        status=Media.Status.DONE,
        sha256=sha256,
        width=640,
        height=360,
    )
    for index, pdq, quality in frames:
        FrameHash.objects.create(media=media, frame_index=index, pdq=pdq, quality=quality)
    return media


def frames_of(media: Media) -> list[tuple[int, bytes, int]]:
    return [
        (index, bytes(pdq), quality)
        for index, pdq, quality in media.hashes.order_by("frame_index").values_list(
            "frame_index", "pdq", "quality"
        )
    ]


def process_claimed(cdn: Cdn) -> tuple[Media, MediaWorker]:
    media = queue.claim_media()
    assert media is not None
    worker = cdn.worker()
    worker.process(media)
    media.refresh_from_db()
    return media, worker


def test_content_keys() -> None:
    hq, full = media_content_key(SITE02), media_content_key(SITE04)
    assert hq is not None and hq == full
    assert hq.needle in SITE02 and hq.needle in SITE04
    # The page-level mirror key ignores the suffix; the content key does not.
    frame = SITE02.replace("_main", "_3")
    assert mirror_key(SITE02) == mirror_key(frame) == "imgs/56/95/0a1b2c3d4e5f607"
    assert media_content_key(frame) != hq
    old = media_content_key(TOKENIZED.format(token="FirstToken", expiry=1789498800))
    new = media_content_key(
        TOKENIZED.format(token="AnotherToken", expiry=1789585200).replace("//b2.", "//b3.")
    )
    assert old is not None and old == new
    assert media_content_key(TOKENIZED.format(token="t", expiry=1).replace("full", "small")) is None
    assert (
        media_content_key(TOKENIZED.format(token="t", expiry=1789498800).replace("full", "small"))
        != old
    )
    # Unknown hosts and generic paths are never keys: sites on the same video CMS reuse screenshot ids.
    assert (
        media_content_key(
            TOKENIZED.format(token="t", expiry=1789498800).replace(
                "b2.cdn.site03.example", "evil-cdn.site03.example"
            )
        )
        is None
    )
    assert (
        media_content_key("https://site01.example/contents/videos_screenshots/0/111/preview.jpg")
        is None
    )
    assert media_content_key("data:image/png;base64,xyz") is None


def test_mirror_media_reused_before_download(page: Page, mirror_page: Page) -> None:
    source = hashed_media(mirror_page, SITE04)
    target = Media.objects.create(
        page=page, kind=Media.Kind.FRAME, url=SITE02, eligible_at=timezone.now()
    )
    cdn = Cdn()

    media, worker = process_claimed(cdn)

    assert media.pk == target.pk
    assert cdn.requested == []
    assert media.status == Media.Status.DONE and media.last_error == ""
    assert frames_of(media) == frames_of(source)
    # Nothing was downloaded: no sha256 or size is claimed for the copy.
    assert (media.sha256, media.byte_size, media.fetched_at) == ("", None, None)
    reuse = MediaReuse.objects.get(media=media)
    assert (reuse.source_id, reuse.reason, reuse.frames) == (source.pk, "url_key", 2)
    assert reuse.key == "mirror:imgs/56/95/0a1b2c3d4e5f607_main.jpg"
    assert worker.reused == {"url_key": 1, "sha256": 0}


def test_rotated_token_url_reused_on_recrawl(page: Page) -> None:
    source = hashed_media(page, TOKENIZED.format(token="old", expiry=1789498800))
    Media.objects.create(
        page=page,
        kind=Media.Kind.POSTER,
        url=TOKENIZED.format(token="new", expiry=1789585200),
        eligible_at=timezone.now(),
    )
    cdn = Cdn()

    media, _ = process_claimed(cdn)

    assert cdn.requested == []
    assert frames_of(media) == frames_of(source)
    assert MediaReuse.objects.get(media=media).reason == MediaReuse.Reason.URL_KEY


@pytest.mark.parametrize("status", [Page.SafetyStatus.REVIEW, Page.SafetyStatus.BLOCKED])
def test_no_reuse_from_flagged_pages(
    page: Page, mirror_page: Page, status: Page.SafetyStatus
) -> None:
    hashed_media(mirror_page, SITE04)
    Page.objects.filter(pk=mirror_page.pk).update(safety_status=status)
    target = Media.objects.create(
        page=page, kind=Media.Kind.POSTER, url=SITE02, eligible_at=timezone.now()
    )
    assert queue.find_url_source(target) is None
    cdn = Cdn()

    media, _ = process_claimed(cdn)

    assert cdn.requested == ["https://fastdelivery.site02.example/robots.txt", SITE02]
    assert not MediaReuse.objects.exists()
    assert media.status != Media.Status.DONE


def test_no_reuse_into_a_page_flagged_after_claim(page: Page, mirror_page: Page) -> None:
    source = hashed_media(mirror_page, SITE04)
    Media.objects.create(
        page=page, kind=Media.Kind.POSTER, url=SITE02, eligible_at=timezone.now()
    )
    media = queue.claim_media()
    assert media is not None
    Page.objects.filter(pk=page.pk).update(safety_status=Page.SafetyStatus.BLOCKED)

    assert queue.reuse_hashes(media, source, reason=MediaReuse.Reason.URL_KEY, key="k")

    media.refresh_from_db()
    assert (media.status, media.last_error) == (Media.Status.SKIPPED, SAFETY_BLOCK_REASON)
    assert not media.hashes.exists()
    assert not MediaReuse.objects.exists()


def test_sources_need_frame_hashes_and_the_same_decoder(page: Page, mirror_page: Page) -> None:
    empty = hashed_media(mirror_page, SITE04, frames=())
    target = Media.objects.create(
        page=page, kind=Media.Kind.POSTER, url=SITE02, eligible_at=timezone.now()
    )
    assert queue.find_url_source(target) is None
    assert queue.find_sha256_source(target, empty.sha256) is None

    clip = hashed_media(mirror_page, "https://cdn.test/clip.mp4", kind=Media.Kind.PREVIEW)
    assert queue.find_sha256_source(target, clip.sha256) is None
    # Posters and hover frames are decoded alike.
    image = hashed_media(mirror_page, "https://cdn.test/frame.jpg", kind=Media.Kind.FRAME)
    assert queue.find_sha256_source(target, image.sha256) == image
    # A photo post's image is the work itself (issue #69), but the same decoder reads it, so the
    # same bytes give the same frames and its hashes may be reused for a poster.
    photo = hashed_media(
        mirror_page, "https://n1.site11.example/content/a/b/c/1000/c_0040.jpg", kind=Media.Kind.PHOTO
    )
    Media.objects.filter(pk=photo.pk).update(sha256="b" * 64)
    assert queue.find_sha256_source(target, "b" * 64) == photo


def test_identical_bytes_reuse_hashes_without_pdq(
    page: Page, mirror_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = to_bytes(synthetic_image(3))
    digest = hashlib.sha256(body).hexdigest()
    source = hashed_media(mirror_page, "https://other.test/poster.png", sha256=digest)
    url = "https://cdn.example.test/copy.png"
    Media.objects.create(page=page, kind=Media.Kind.POSTER, url=url, eligible_at=timezone.now())

    def no_pdq(kind: Media.Kind, data: bytes) -> media_worker.HashedMedia:
        raise AssertionError("PDQ must not run for known bytes")

    monkeypatch.setattr(media_worker, "hash_media", no_pdq)
    cdn = Cdn({url: (200, body, "image/png")})

    media, worker = process_claimed(cdn)

    assert cdn.requested == ["https://cdn.example.test/robots.txt", url]
    assert media.status == Media.Status.DONE
    assert frames_of(media) == frames_of(source)
    assert (media.sha256, media.byte_size, media.content_type) == (digest, len(body), "image/png")
    assert (media.width, media.height) == (640, 360)
    assert media.fetched_at is not None
    reuse = MediaReuse.objects.get(media=media)
    assert (reuse.source_id, reuse.reason, reuse.key) == (source.pk, "sha256", digest)
    assert worker.reused == {"url_key": 0, "sha256": 1}


def test_unknown_bytes_are_hashed_and_rehash_drops_the_reuse_record(page: Page) -> None:
    body = to_bytes(synthetic_image(4))
    url = "https://cdn.example.test/new.png"
    Media.objects.create(page=page, kind=Media.Kind.POSTER, url=url, eligible_at=timezone.now())
    media, _ = process_claimed(Cdn({url: (200, body, "image/png")}))
    assert media.status == Media.Status.DONE and media.hashes.count() == 1
    assert not MediaReuse.objects.exists()

    MediaReuse.objects.create(media=media, reason=MediaReuse.Reason.SHA256)
    queue.store_hashes(media, [], sha256="b" * 64)
    assert not MediaReuse.objects.exists()
