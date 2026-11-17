"""The only place a browser may be pointed at a page (ADR 0016, mirrors `index/media_fetch.py`).

The capture deny is not a property of the triage queue or of the worker's in-memory copy of a
page: it is a property of the visit itself. `capture_page` re-reads the page's authoritative
safety state, its availability and its site's enabled flag from the database *immediately before*
handing the URL to the browser, and raises `CaptureDenied` instead of opening a browser.

That ordering is what makes the invariant testable: a page blocked, put under review, gated or
taken down *after* its candidate row was claimed still cannot cause a browser visit, because the
decision is taken after the claim and inside the same call that would otherwise start the
browser. A queue filter is defence in depth, never the guarantee.
"""

from collections.abc import Callable

from index.models import Page
from index.services import media_denial_reason, page_media_allowed
from marceline.capture.runner import (
    BrowserRunner,
    CaptureOutcome,
    CaptureRequest,
    Decision,
    Loaded,
    RequestCheck,
)

# Stable capture skip reasons (stored in BrowserCaptureCandidate.last_error / capture_run).
PAGE_GONE_REASON = "page row is gone"
REMOVED_REASON = "page removed"
SITE_DISABLED_REASON = "site disabled"
# The per-site capture switch (ADR 0016 amendment, issue #78): distinct from SITE_DISABLED_REASON
# — the site keeps crawling, only browser capture is off for it, by operator decision.
CAPTURE_DISABLED_REASON = "site excluded from browser capture"


class CaptureDenied(Exception):
    """This page must not be visited with a browser. `reason` is a stable skip reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def capture_denial(page_id: int) -> str | None:
    """Why this page must not be captured right now, or None when it may be.

    Reads the current state, never the caller's in-memory copy of it. The safety rule is the same
    one the media gate uses (`index.services.page_media_allowed`, ADR 0018): a browser pulls far
    more of a page than a poster fetch does, so it can never be allowed where a poster is not.
    """
    row = (
        Page.objects.filter(pk=page_id)
        .values_list(
            "safety_status",
            "safety_score",
            "availability",
            "site__enabled",
            "site__capture_enabled",
        )
        .first()
    )
    if row is None:
        return PAGE_GONE_REASON
    safety_status, safety_score, availability, site_enabled, site_capture_enabled = row
    if not page_media_allowed(safety_status, safety_score):
        return media_denial_reason(safety_status, safety_score)
    if availability == Page.Availability.REMOVED:
        return REMOVED_REASON
    if not site_enabled:
        return SITE_DISABLED_REASON
    if not site_capture_enabled:
        return CAPTURE_DISABLED_REASON
    return None


def capture_denial_detail(page_id: int, rule: str) -> dict[str, object]:
    """The reconstructable detail of a safety/availability deny: page id and the rule name only.

    Never the page's text or URL — findings name identifiers, never content. Stored on
    `BrowserCaptureCandidate.refusal_detail`, never in `capture_run`, for the same reason as
    `marceline.capture.requests.page_robots_refusal`: this skip never opened a browser and must
    stay retryable, which `capture_run`'s emptiness is what marks (ADR 0016's Scan once rule).
    """
    return {"kind": "capture_denied", "page_id": page_id, "rule": rule}


def capture_page(
    runner: BrowserRunner,
    *,
    page_id: int,
    request: CaptureRequest,
    decide: Callable[[Loaded], Decision],
    check: RequestCheck,
) -> CaptureOutcome:
    """Visit this page with the browser, or raise `CaptureDenied` without starting one.

    `check` is asked before every request the page makes (robots.txt per host, embed allowlist;
    `marceline.capture.requests`), so the visit follows the crawler's policy request by request.
    """
    denial = capture_denial(page_id)
    if denial is not None:
        raise CaptureDenied(denial)
    return runner.capture(request, decide, check)
