"""Image passthrough: the reviewer's browser never contacts a piracy site or CDN.

Only existing poster/frame `index.Media` rows are fetched (no caller-supplied URLs), never for
pages in safety review or blocked, through `core.http.PoliteClient` with the project User-Agent.
Bytes stay in memory for one response (`Cache-Control: no-store`) and are never written anywhere.

The server makes the request, so the server must not be talked into asking itself or its network
(SSRF). The client is `public_only`: the media URL and **every redirect hop** must be http(s) to a
host with only public addresses, checked before the hop is requested, and the connection goes to
the address that was checked (no DNS rebinding). A refused host is a 403.

The media host's own robots.txt applies here as in the media worker (ADR 0007 amendment
2026-09-17): the same `matching.media_robots` check runs after the deny, before every hop, with
rules cached per web process for 24 h and fetched (counted as `robots`) when missing. A disallowed
image is a 403; an unreadable robots.txt a 502.

Errors say that the image is unavailable and, in broad terms, why — never how the upstream server
answered (status code, error text): the reviewer does not need it, and it would turn the endpoint
into a probe of whatever the URL points at.
"""

import threading
import time
from collections import deque
from collections.abc import Callable

import httpx
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from ninja import Router, Schema, Status

from catalog.request_accounting import RequestAccounting
from core.http import AddressNotAllowedError, FetchResponse, PoliteClient, ResponseTooLargeError
from core.robots import InvalidRobotsUrlError
from index.media_fetch import MediaFetchDenied, fetch_media
from index.models import Media
from marceline.review.auth import staff_auth
from marceline.review.common import IMAGE_KINDS, media_allowed
from matching.media_robots import MediaRobots, MediaRobotsDisallowed, MediaRobotsUnavailable

router = Router(tags=["media"], auth=staff_auth)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
FETCH_TIMEOUT_S = 10.0
#: A CDN may bounce an image through a signed URL or two; more hops than this is not an image.
MAX_REDIRECTS = 3
RATE_LIMIT_REQUESTS = 20
RATE_LIMIT_WINDOW_S = 1.0
EXPIRED_STATUSES = frozenset({401, 403, 404, 410})
BLOCKED_IMAGE_TYPES = frozenset({"image/svg+xml"})


class MediaErrorOut(Schema):
    detail: str
    reason: str


class RateLimiter:
    """Sliding-window limit per key, in process memory (one per web worker process)."""

    def __init__(
        self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_S)
#: Media hosts' robots.txt, per web process; the client is handed over per request.
robots = MediaRobots(None, str(settings.CRAWLER_USER_AGENT))


def build_client() -> PoliteClient:
    """A `public_only` client (SSRF guard, every hop). Tests swap in a `httpx.MockTransport`."""
    return PoliteClient(
        settings.CRAWLER_USER_AGENT,
        timeout_s=FETCH_TIMEOUT_S,
        max_bytes=MAX_IMAGE_BYTES,
        max_retries=0,
        max_redirects=MAX_REDIRECTS,
        public_only=True,
        # Counted as `review_image` for the page's site, never refused by a budget (issue #74).
        observer=RequestAccounting(),
    )


def _error(status: int, detail: str, reason: str) -> Status[MediaErrorOut]:
    return Status(status, MediaErrorOut(detail=detail, reason=reason))


IMAGE_RESPONSES = {
    403: MediaErrorOut,
    404: MediaErrorOut,
    429: MediaErrorOut,
    502: MediaErrorOut,
}
IMAGE_OPENAPI = {
    "responses": {
        "200": {
            "description": "Image bytes (image/*), Cache-Control: no-store",
            "content": {"image/*": {"schema": {"type": "string", "format": "binary"}}},
        }
    }
}


@router.get("/{media_id}/image", response=IMAGE_RESPONSES, openapi_extra=IMAGE_OPENAPI)
def media_image(request: HttpRequest, media_id: int) -> HttpResponse | Status[MediaErrorOut]:
    media = Media.objects.select_related("page").filter(pk=media_id).first()
    if media is None or media.kind not in IMAGE_KINDS:
        return _error(404, "image unavailable", "unknown media")
    if not media_allowed(media.page):
        return _error(403, "image unavailable", "page is flagged or gated for safety")
    if not limiter.allow(str(request.user.pk)):
        return _error(429, "image unavailable", "too many image requests")
    try:
        with build_client() as client:
            # Same chokepoint as the media worker: the deny is re-checked against the database
            # inside this call, so it cannot be bypassed from here either (issue #65).
            response: FetchResponse = fetch_media(
                client,
                media,
                max_bytes=MAX_IMAGE_BYTES,
                enforce_budget=False,
                check=robots.check_for(media, 0.0, client=client),
            )
    except MediaFetchDenied:
        return _error(403, "image unavailable", "page is flagged or gated for safety")
    except MediaRobotsDisallowed:
        return _error(403, "image unavailable", "media host disallows us in robots.txt")
    except (MediaRobotsUnavailable, InvalidRobotsUrlError):
        return _error(502, "image unavailable", "upstream unavailable")
    except AddressNotAllowedError:
        return _error(403, "image unavailable", "media host not allowed")
    except ResponseTooLargeError:
        return _error(502, "image unavailable", "larger than 5 MB")
    except httpx.HTTPError:
        return _error(502, "image unavailable", "upstream unavailable")
    if response.status in EXPIRED_STATUSES:
        return _error(404, "image unavailable", "expired or removed")
    if response.status != 200:
        return _error(502, "image unavailable", "upstream unavailable")
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if not content_type.startswith("image/") or content_type in BLOCKED_IMAGE_TYPES:
        return _error(502, "image unavailable", "upstream did not return an image")
    result = HttpResponse(response.body, content_type=content_type)
    result["Cache-Control"] = "no-store"
    result["X-Content-Type-Options"] = "nosniff"
    result["Content-Security-Policy"] = "default-src 'none'; sandbox"
    result["Referrer-Policy"] = "no-referrer"
    return result
