"""The only place preview media may be requested over the network (ADR 0018, issue #65).

The media deny is not a property of the worker's queue query or of the moderation UI: it is a
property of the fetch itself. Every caller that wants media bytes — the media worker and the
review image passthrough — goes through `fetch_media`, which re-reads the page's authoritative
safety state and the media row's own eligibility from the database *immediately before* the
request and refuses when either says no.

That ordering is what makes the invariant testable: a page blocked, put under review or gated
after its media was claimed still cannot cause an HTTP request, because the decision is taken
after the claim and inside the same call that would otherwise open the socket. "Immediately
before" is literal: the check is `PoliteClient`'s `before_request` hook, which runs after the
per-host wait and before every attempt — each retry and each redirect hop — so a page blocked
while the worker slept for its host's slot is not requested either.
"""

from collections.abc import Callable

from catalog.request_accounting import MEDIA, REVIEW_IMAGE
from core.http import FetchResponse, PoliteClient, RequestLabel, UrlHook
from index.models import Media
from index.services import media_denial_reason, page_media_allowed


class MediaFetchDenied(Exception):
    """A media row must not be requested. `reason` is a stable media skip reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def media_denial(media_id: int) -> str | None:
    """Why this media must not be fetched right now, or None when it may be.

    Reads the current state, never the caller's in-memory copy of it.
    """
    row = (
        Media.objects.filter(pk=media_id)
        .values_list("page__safety_status", "page__safety_score", "eligible_at")
        .first()
    )
    if row is None:
        return "media row is gone"
    safety_status, safety_score, eligible_at = row
    if not page_media_allowed(safety_status, safety_score):
        return media_denial_reason(safety_status, safety_score)
    if eligible_at is None:
        # The page is clearly safe but this row was never released (listing card media whose
        # detail page has not been parsed yet).
        return media_denial_reason(safety_status, None)
    return None


def fetch_media(
    client: PoliteClient,
    media: Media,
    *,
    min_delay_s: float | Callable[[], float] = 0.0,
    max_bytes: int | None = None,
    enforce_budget: bool = True,
    check: UrlHook | None = None,
) -> FetchResponse:
    """GET this media's URL, or raise `MediaFetchDenied` without touching the network.

    The request is labelled `media` for the page's site, so the client's request accounting
    counts it and applies the origin media or CDN host budget (issue #74) — a separate check
    that runs after `deny`: a denied request never looks at a budget. `enforce_budget=False`
    counts without refusing (the review image passthrough).

    `check` is a further refusal hook run right after the deny, before every attempt and redirect
    hop (the media worker's robots.txt check, which may itself fetch a robots.txt — only ever
    after the deny passed). A callable `min_delay_s` is evaluated after the first checks, so a
    delay they learn (a robots.txt Crawl-delay) already applies to this request.
    """

    def deny(url: str) -> None:
        # Checks that must hold immediately before every attempt go here, in order.
        denial = media_denial(media.pk)
        if denial is not None:
            raise MediaFetchDenied(denial)
        if check is not None:
            check(url)

    deny(media.url)  # no point waiting for a host slot that will not be used
    delay_s = min_delay_s() if callable(min_delay_s) else min_delay_s
    label = RequestLabel(
        REVIEW_IMAGE if not enforce_budget else MEDIA,
        site_id=media.page.site_id,
        enforce=enforce_budget,
    )
    return client.get(
        media.url,
        min_delay_s=delay_s,
        max_bytes=max_bytes,
        before_request=deny,
        # A redirect target's robots.txt is fetched here, before that host's turn is taken, so the
        # check inside the turn finds it cached instead of fetching under the host's own lease
        # (ADR 0030). The check still runs again immediately before the request.
        on_redirect=check,
        label=label,
    )
