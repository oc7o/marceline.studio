"""robots.txt for media hosts (ADR 0007 amendment 2026-09-17).

A poster, hover frame or preview clip is requested from whatever host serves it — the site's own
server or a CDN — and that host's own robots.txt decides, for our User-Agent, exactly as a site's
robots.txt decides for its pages. The check is `MediaRobots.check_for(media, delay)`, passed to
`index.media_fetch.fetch_media(check=...)`: it runs once before the host wait and then in the
client's `before_request` hook before *every* attempt and redirect hop, after the media deny and
before the request budget, so a disallowed request is never sent and never uses budget.

- Rules are cached per origin in this process for `ROBOTS_MAX_AGE_S` (24 h), like the crawler's.
- robots.txt answering 4xx (404 included) allows everything (RFC 9309).
- Disallowed: `MediaRobotsDisallowed`; the worker marks the row `robots_blocked`, never requested.
- 5xx, 429 or a network error: `MediaRobotsUnavailable`; nothing is requested from that host, the
  row goes back with its attempt refunded and the host's other pending media wait with it, backing
  off per host like a paused site (`RETRY_BASE_DELAY` 1 min doubling up to `RETRY_MAX_DELAY` 6 h).
- robots.txt requests are counted (`kind=robots` for the media's site, issue #74) but never refused
  by a budget: one per host per worker a day, and refusing it would hold a site's media behind its
  page budget, which the crawler uses up every day.
- A robots.txt `Crawl-delay` of the media host raises the delay between its media requests.
"""

import time
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import urlsplit

from catalog.request_accounting import ROBOTS
from core.http import FetchResponse, PoliteClient, RequestLabel, RequestRefusedError, UrlHook
from core.robots import ROBOTS_MAX_AGE_S, RobotsCache, RobotsUnavailableError
from index.models import Media
from matching.media import retry_delay

ROBOTS_DISALLOWED = "disallowed by robots.txt"
REDIRECT_DISALLOWED = "redirect target disallowed by robots.txt"
ROBOTS_UNAVAILABLE = "robots.txt unavailable"


class MediaRobotsDisallowed(RequestRefusedError):
    """The media host's robots.txt disallows this URL for our User-Agent; nothing was sent."""


class MediaRobotsUnavailable(RequestRefusedError):
    """The media host's robots.txt could not be read; nothing may be requested from `host` before
    `retry_in` has passed."""

    def __init__(self, reason: str, *, host: str, retry_in: timedelta) -> None:
        super().__init__(reason)
        self.host = host
        self.retry_in = retry_in


class MediaRobots:
    """Per-process robots.txt cache and per-host backoff for media fetches.

    The media worker passes its one client here; the review image passthrough, which builds a
    client per request, passes `client=None` and hands its client to each `check_for`.
    """

    def __init__(
        self,
        client: PoliteClient | None,
        user_agent: str,
        *,
        max_age_s: float = ROBOTS_MAX_AGE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._failures: dict[str, int] = {}
        self._cache = RobotsCache(self._no_client, user_agent, max_age_s=max_age_s, clock=clock)

    def check_for(
        self, media: Media, min_delay_s: float, *, client: PoliteClient | None = None
    ) -> UrlHook:
        """A hook refusing any URL of this media (or a redirect hop) its host's robots.txt does
        not allow. Fetches the host's robots.txt first when it is not cached, through `client`
        (default: the one given at construction), counted as `robots` for the media's site."""
        fetching = client or self._client
        label = RequestLabel(ROBOTS, site_id=media.page.site_id, enforce=False)

        def fetch(url: str, delay_s: float) -> FetchResponse:
            if fetching is None:
                raise RuntimeError("MediaRobots.check_for needs a client")
            return fetching.get(url, min_delay_s=delay_s, label=label)

        def check(url: str) -> None:
            host = (urlsplit(url).hostname or "").lower()
            try:
                rules = self._cache.rules_for(url, min_delay_s=min_delay_s, fetch=fetch)
            except RobotsUnavailableError as exc:
                failures = self._failures[host] = self._failures.get(host, 0) + 1
                raise MediaRobotsUnavailable(
                    f"{ROBOTS_UNAVAILABLE}: {exc}", host=host, retry_in=retry_delay(failures)
                ) from exc
            self._failures.pop(host, None)
            if not rules.can_fetch(url):
                if url == media.url:
                    raise MediaRobotsDisallowed(ROBOTS_DISALLOWED)
                raise MediaRobotsDisallowed(f"{REDIRECT_DISALLOWED}: {url}")

        return check

    def crawl_delay_s(self, url: str) -> float:
        """The cached robots.txt Crawl-delay of `url`'s host, 0 when unknown; never fetches."""
        rules = self._cache.cached(url)
        return (rules.crawl_delay_s or 0.0) if rules is not None else 0.0

    def clear(self) -> None:
        """Forget every cached robots.txt and failure streak (tests)."""
        self._cache.clear()
        self._failures.clear()

    @staticmethod
    def _no_client(url: str, min_delay_s: float) -> FetchResponse:
        raise RuntimeError("MediaRobots fetches only through check_for")
