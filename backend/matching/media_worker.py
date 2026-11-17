"""Media worker: fetch preview media soon after the crawl, PDQ-hash it, keep only the hashes.

The downloaded bytes live in memory for one iteration and are never stored (frames.py uses a
temporary directory for ffmpeg and removes it).
"""

import hashlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.utils import timezone

from catalog import media_shapes
from catalog.models import HostRole
from catalog.request_accounting import MEDIA, HostRoles
from catalog.services import effective_crawl_delay_s
from core.http import (
    HOST_DOES_NOT_RESOLVE,
    AddressNotAllowedError,
    BudgetExhaustedError,
    FetchResponse,
    HostBusyError,
    PoliteClient,
    RequestLabel,
    ResponseTooLargeError,
)
from core.log_scrub import scrub_urls
from core.robots import InvalidRobotsUrlError
from index.media_fetch import MediaFetchDenied, fetch_media
from index.models import Media
from index.services import media_denial_reason, page_media_allowed
from matching import media as queue
from matching.media_hash import (
    PREVIEW_DUPLICATE_DISTANCE,
    PREVIEW_FPS,
    PREVIEW_MAX_FRAMES,
    STILL_IMAGE_KINDS,
    HashedMedia,
    HasherUnavailableError,
    UnsupportedMediaError,
    hash_media_bytes,
)
from matching.media_robots import MediaRobots, MediaRobotsDisallowed, MediaRobotsUnavailable
from matching.models import MediaReuse
from matching.pdq import MediaDecodeError

__all__ = [
    "PREVIEW_DUPLICATE_DISTANCE",
    "PREVIEW_FPS",
    "PREVIEW_MAX_FRAMES",
    "STILL_IMAGE_KINDS",
    "HashedMedia",
    "MediaWorker",
    "UnsupportedMediaError",
    "hash_media",
    "media_delay_s",
    "slot_media_delay_s",
]

logger = logging.getLogger(__name__)

IMAGE_MAX_BYTES = 5 * 1024 * 1024
VIDEO_MAX_BYTES = 20 * 1024 * 1024
MAX_BYTES: dict[str, int] = {
    Media.Kind.POSTER: IMAGE_MAX_BYTES,
    Media.Kind.FRAME: IMAGE_MAX_BYTES,
    Media.Kind.SPRITE: IMAGE_MAX_BYTES,
    Media.Kind.PREVIEW: VIDEO_MAX_BYTES,
    Media.Kind.PHOTO: IMAGE_MAX_BYTES,
    # Never fetched (see `process`); present so the table stays total over Media.Kind.
    Media.Kind.CAPTURE: IMAGE_MAX_BYTES,
}
# Browser-capture rows (ADR 0016) carry frames the capture worker already hashed; their URL is
# the page itself, not media. This worker must never request one.
CAPTURE_NOT_FETCHED = "browser capture frames are never fetched over HTTP"
CLIENT_MAX_BYTES = max(MAX_BYTES.values())
# Media answering 403/404/410 is gone: retrying cannot help.
GONE_STATUSES = frozenset({403, 404, 410})
# Skip reason suffix for GONE_STATUSES on a tokenized link (`media.EXPIRING_URL_REGEX`) — only
# those expire, and a re-crawl of the page hands out a fresh one. crawler.services.
# recover_expired_media matches it to re-crawl the page that produced the link (crawler may not
# import matching, so it is repeated there).
LINK_EXPIRED = "link expired or gone"
# Failure reason suffix for GONE_STATUSES on any other URL: a plain poster that answers 404 is just
# gone, and re-crawling its page would only fetch the same dead link again (review H3: site17.example
# spent 957 of 1253 detail fetches a day on such re-crawls).
MEDIA_GONE = "media gone"
_EXPIRING_URL = re.compile(queue.EXPIRING_URL_REGEX)
# Skip (or, for a name that does not resolve, retry) reason prefix when the client refuses a hop
# that is not http(s) to public addresses only (`core.http` `public_only`).
ADDRESS_NOT_ALLOWED = "address not allowed"


def is_expiring_url(url: str) -> bool:
    """True for tokenized media links that stop working soon after the crawl."""
    return _EXPIRING_URL.search(url) is not None


# Legacy per-site workers (in-memory delays): the crawler fetches pages from the site's own host
# at the same time, so media served from that same host waits twice as long and crawler + media
# worker together stay at <= 1.5 requests per delay window there. CDN hosts (other hostnames, incl.
# subdomains) get the plain delay. The crawl runtime uses `slot_media_delay_s` instead.
SITE_HOST_DELAY_FACTOR = 2.0


class Hasher(Protocol):
    """Hashing of media bytes: in this thread, or in a hash child (ADR 0030)."""

    def __call__(
        self,
        kind: str,
        data: bytes,
        *,
        crop_bars: bool = False,
        sprite_grid: tuple[int, int] | None = None,
    ) -> HashedMedia: ...


DelayFor = Callable[[Media], float]


def media_delay_s(media: Media) -> float:
    """Minimum seconds between media requests to this media's host (legacy per-site worker)."""
    site = media.page.site
    delay = effective_crawl_delay_s(site)
    host = (urlsplit(media.url).hostname or "").lower()
    domain = site.domain.lower()
    if host in {domain, f"www.{domain}"}:
        return delay * SITE_HOST_DELAY_FACTOR
    return delay


def slot_media_delay_s(media: Media, roles: HostRoles, *, cdn_delay_s: float) -> float:
    """Minimum seconds between requests to this media's host on database host slots (ADR 0030).

    Origin hosts (the site's own servers, `SiteHost` overrides first) share one slot with the
    site's pages, so media there waits the site's delay like a page — no doubling. A CDN host has
    its own slot at `cdn_delay_s` (`CDN_HOST_DELAY_S`). A media host's robots.txt Crawl-delay
    and the slot's operator floor raise either on top (`MediaWorker`, `core.HostSlot`).
    """
    site = media.page.site
    host = (urlsplit(media.url).hostname or "").lower()
    role = roles.role(RequestLabel(MEDIA, site_id=site.pk), host)
    if role == HostRole.ORIGIN:
        return effective_crawl_delay_s(site)
    return cdn_delay_s


def hash_media(
    kind: str,
    data: bytes,
    *,
    crop_bars: bool = False,
    embed: bool = False,
    model_path: str | None = None,
    sprite_grid: tuple[int, int] | None = None,
) -> HashedMedia:
    """PDQ frames for downloaded media bytes. Raises MediaDecodeError for undecodable input."""
    return hash_media_bytes(
        str(kind),
        data,
        crop_bars=crop_bars,
        embed=embed,
        model_path=model_path,
        sprite_grid=sprite_grid,
    )


class MediaWorker:
    def __init__(
        self,
        client: PoliteClient,
        *,
        on_result: Callable[[Media], None] | None = None,
        user_agent: str | None = None,
        robots: MediaRobots | None = None,
        hasher: Hasher | None = None,
        delay_for: DelayFor | None = None,
        crop_bars: bool | None = None,
        embed: bool | None = None,
    ) -> None:
        """`robots` shares one robots.txt cache between workers (the runtime's threads; each
        fetch still goes through this worker's client), `hasher` replaces in-thread hashing (the
        runtime's hash processes) and `delay_for` the legacy per-site delay rule.

        `crop_bars` overrides `settings.MATCHING_CROP_BLACK_BARS` (off by default,
        docs/matching/black-bars.md): letterbox/pillarbox padding is trimmed before hashing, so
        newly hashed media carries de-barred hashes while everything already stored does not.

        `embed` overrides `settings.MATCHING_EMBEDDINGS_ENABLED` (off by default, ADR 0037): the
        second signal's 512-bit sketch is computed from the same picture PDQ sees. It applies to
        in-thread hashing only — a `hasher` (the runtime's hash pool) was built with its own
        answer, because the model is loaded once per process, not once per media."""
        self._client = client
        self._on_result = on_result
        # Media hosts' own robots.txt (ADR 0007 amendment 2026-09-17), checked before every attempt.
        self._robots = robots or MediaRobots(None, user_agent or str(settings.CRAWLER_USER_AGENT))
        self._hasher = hasher
        self._delay_for = delay_for or media_delay_s
        self._crop_bars = (
            bool(settings.MATCHING_CROP_BLACK_BARS) if crop_bars is None else crop_bars
        )
        self._embed = bool(settings.MATCHING_EMBEDDINGS_ENABLED) if embed is None else embed
        self._model_path = str(settings.MATCHING_EMBEDDING_MODEL_PATH) or None
        # Media finished with copied frame hashes (no download or no hashing), per reason.
        self.reused: dict[str, int] = {reason: 0 for reason in MediaReuse.Reason.values}

    def run(
        self,
        *,
        site_domain: str | None = None,
        limit: int | None = None,
        once: bool = False,
        idle_sleep_s: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> int:
        """Process media until `limit` is reached; with `once` or `limit`, stop when idle."""
        drain = once or limit is not None
        processed = 0
        queue.recover_stale_media()
        while limit is None or processed < limit:
            media = queue.claim_media(site_domain=site_domain)
            if media is None:
                if drain:
                    break
                sleep(idle_sleep_s)
                queue.recover_stale_media()
                continue
            self.process_claimed(media)
            processed += 1
        return processed

    def process_claimed(self, media: Media) -> None:
        """`process` a claimed row; an unexpected error fails this row instead of the worker."""
        try:
            self.process(media)
        except Exception as exc:  # one bad row must not stop the worker (review M5)
            logger.exception("processing media %s failed", media.pk)
            queue.fail_media(
                media, f"worker error: {type(exc).__name__}: {exc}", retry=False, fetched=False
            )
        if self._on_result is not None:
            self._on_result(media)

    def process(self, media: Media) -> None:
        """Handle one claimed media row; always leaves it in a non-RUNNING state."""
        kind = Media.Kind(media.kind)
        # Cheap pre-check on what the caller already knows. The binding decision is taken inside
        # `fetch_media` against the database, immediately before the request (ADR 0018).
        if not page_media_allowed(media.page.safety_status, media.page.safety_score):
            reason = media_denial_reason(media.page.safety_status, media.page.safety_score)
            queue.skip_media(media, reason, fetched=False)
            return
        if kind == Media.Kind.CAPTURE:
            queue.skip_media(media, CAPTURE_NOT_FETCHED, fetched=False)
            return
        if urlsplit(media.url).scheme not in {"http", "https"}:
            queue.skip_media(media, "not an http(s) URL", fetched=False)
            return
        found = queue.find_url_source(media)
        if found is not None and self._reuse(media, found[0], MediaReuse.Reason.URL_KEY, found[1]):
            return

        # This URL's *shape* keeps answering 404 on this site (ADR 0041): the row is postponed,
        # never skipped, and nothing is requested. Last of the checks that cost no request, so a
        # reuse that needs none still happens; the safety gate is re-read inside the fetch anyway.
        shape = media_shapes.shape_of(media.url)
        until = media_shapes.admit(media.page.site_id, media.url)
        if until is not None:
            queue.hold_media(media, media_shapes.suppression_reason(shape, until), until)
            return

        delay_s = self._delay_for(media)
        try:
            response = fetch_media(
                self._client,
                media,
                # After the checks: the host's robots.txt Crawl-delay is known by then.
                min_delay_s=lambda: max(delay_s, self._robots.crawl_delay_s(media.url)),
                max_bytes=MAX_BYTES[kind],
                check=self._robots.check_for(media, delay_s, client=self._client),
            )
        except MediaFetchDenied as denied:
            # The page was flagged or gated after this media was claimed: no request was made.
            queue.skip_media(media, denied.reason, fetched=False)
            return
        except HostBusyError as busy:
            # The host's database slot stayed taken (ADR 0030): nothing was sent, try again later.
            held = queue.defer_media(
                media, busy.reason, host=busy.host, until=busy.retry_at, site_id=media.page.site_id
            )
            logger.info("%s; %d more media of this site on %s wait", busy.reason, held, busy.host)
            return
        except BudgetExhaustedError as exhausted:
            # Origin media or CDN host budget used up (issue #74): no request was made.
            held = queue.defer_media(
                media,
                exhausted.reason,
                host=exhausted.host,
                until=exhausted.resume_at,
                site_id=None if exhausted.scope == HostRole.CDN else media.page.site_id,
            )
            logger.info("%s; %d more media on %s wait", exhausted.reason, held, exhausted.host)
            return
        except MediaRobotsDisallowed as disallowed:
            # The media host's robots.txt says no: never requested, never retried.
            queue.robots_block_media(media, disallowed.reason)
            return
        except MediaRobotsUnavailable as unavailable:
            # No robots.txt, no media from that host: wait like a paused site, attempt refunded.
            held = queue.defer_media(
                media,
                unavailable.reason,
                host=unavailable.host,
                until=timezone.now() + unavailable.retry_in,
            )
            # `unavailable.reason` wraps a `RobotsUnavailableError`, which quotes the robots.txt
            # origin it could not fetch; the row above keeps the full text, the log does not.
            logger.info(
                "%s; %d more media on %s wait",
                scrub_urls(unavailable.reason),
                held,
                unavailable.host,
            )
            return
        except InvalidRobotsUrlError as invalid:
            queue.skip_media(media, f"invalid URL: {invalid}", fetched=False)
            return
        except AddressNotAllowedError as refused:
            # `public_only`: this hop (the URL or a redirect target) is not a public address; no
            # request was made to it. A name that does not resolve may be DNS trouble: retry.
            reason = f"{ADDRESS_NOT_ALLOWED}: {refused.reason}"
            if refused.reason == HOST_DOES_NOT_RESOLVE:
                queue.fail_media(media, reason, fetched=False)
            else:
                queue.skip_media(media, reason, fetched=False)
            return
        except ResponseTooLargeError as exc:
            queue.skip_media(media, f"too large: {exc}")
            return
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            queue.fail_media(media, f"{type(exc).__name__}: {exc}")
            return

        media.content_type = response.headers.get("content-type", "").split(";")[0].strip()[:255]
        media.byte_size = len(response.body)
        self._record_shape(media, shape, response.status)
        if response.status in GONE_STATUSES and is_expiring_url(media.url):
            queue.skip_media(media, f"HTTP {response.status}: {LINK_EXPIRED}")
        elif response.status in GONE_STATUSES:
            queue.fail_media(media, f"HTTP {response.status}: {MEDIA_GONE}", retry=False)
        elif response.status == 429 or response.status >= 500:
            queue.fail_media(media, f"HTTP {response.status}")
        elif response.status >= 400:
            queue.fail_media(media, f"HTTP {response.status}", retry=False)
        elif media.content_type.lower().startswith("text/"):
            queue.skip_media(media, f"not media: {media.content_type}")
        else:
            self._hash_and_store(media, kind, response)

    def _record_shape(self, media: Media, shape: str, status: int) -> None:
        """Tell the shape breaker what this URL's shape answered — 404/410 or 2xx, nothing else.

        Deliberately narrower than the worker's own retry decision (ADR 0041 § 2). A **403** is in
        `GONE_STATUSES` because retrying it cannot help *this* row, but it is an authorization
        answer — hotlink protection, a WAF, a geo-block — not an absence, and none of those makes
        the URL wrong. A 404 on a **tokenized expiring link** is about the token's age, never the
        shape: without that exclusion the measured data suppresses four healthy CDN shapes, one of
        them with a 45-answer run of 404 against 491 successes. A 429 or 5xx says the server is
        unwell, and a shape must never be suppressed for being slow.
        """
        if status in (404, 410) and not is_expiring_url(media.url):
            outcome = media_shapes.GONE
        elif 200 <= status < 300:
            outcome = media_shapes.OK
        else:
            return
        media_shapes.record(media.page.site_id, media.url, outcome)
        logger.debug("media URL shape %s on site %s: %s", shape, media.page.site_id, outcome)

    def _reuse(
        self,
        media: Media,
        source: Media,
        reason: MediaReuse.Reason,
        key: str,
        **fields: Any,
    ) -> bool:
        if not queue.reuse_hashes(media, source, reason=reason, key=key, **fields):
            return False
        if media.status == Media.Status.DONE:
            self.reused[reason] += 1
            logger.info(
                "media %s: frame hashes reused from media %s (%s)", media.pk, source.pk, reason
            )
        return True

    def _hash_and_store(self, media: Media, kind: Media.Kind, response: FetchResponse) -> None:
        sha256 = hashlib.sha256(response.body).hexdigest()
        source = queue.find_sha256_source(media, sha256)
        if source is not None and self._reuse(
            media,
            source,
            MediaReuse.Reason.SHA256,
            sha256,
            sha256=sha256,
            width=source.width,
            height=source.height,
            fetched=True,
        ):
            return
        sprite_grid = (
            (media.sprite_columns, media.sprite_rows)
            if media.sprite_columns is not None and media.sprite_rows is not None
            else None
        )
        try:
            if self._hasher is not None:
                hashed = self._hasher(
                    str(kind), response.body, crop_bars=self._crop_bars, sprite_grid=sprite_grid
                )
            else:
                hashed = hash_media(
                    str(kind),
                    response.body,
                    crop_bars=self._crop_bars,
                    embed=self._embed,
                    model_path=self._model_path,
                    sprite_grid=sprite_grid,
                )
        except UnsupportedMediaError as exc:
            # A sprite whose grid is neither declared nor confidently readable from its own
            # geometry (`matching.sprite_grid`): a deliberate skip, not a failure — retrying the
            # same bytes would answer the same "unknown" every time (issue #14).
            queue.skip_media(media, str(exc))
            return
        except MediaDecodeError as exc:
            queue.fail_media(media, str(exc), retry=False)
            return
        except HasherUnavailableError as exc:
            # The hasher itself broke (a hash child timed out, died or was shut down, ADR 0030):
            # these bytes were never judged, so the row goes back for another try instead of
            # being failed for good. MAX_ATTEMPTS still ends it if it keeps happening.
            logger.warning("hasher unavailable for media %s: %s", media.pk, scrub_urls(str(exc)))
            queue.fail_media(media, f"hash unavailable: {type(exc).__name__}: {exc}")
            return
        except Exception as exc:  # a hashing bug must not stop the worker
            logger.exception(
                "hashing failed for media id=%s site=%s", media.pk, media.page.site.domain
            )
            queue.fail_media(media, f"hash error: {type(exc).__name__}: {exc}", retry=False)
            return
        queue.store_hashes(
            media,
            hashed.frames,
            sha256=sha256,
            width=hashed.width,
            height=hashed.height,
            embedding_pipeline=hashed.embedding_pipeline,
        )
