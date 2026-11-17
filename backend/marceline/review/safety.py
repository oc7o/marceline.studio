"""Safety review queue: pages with `safety_status=review`. Text only — these responses have no
media fields at all, and media of such pages is never served (docs/safety.md)."""

from django.db import transaction
from django.http import HttpRequest
from ninja import Router, Status

from crawler.services import rescore_page
from index.models import Page
from index.services import set_safety_status
from marceline.review.auth import staff_auth
from marceline.review.common import clamp_limit
from marceline.review.schemas import (
    BlockIn,
    ConflictItemOut,
    ConflictOut,
    ErrorOut,
    IdsIn,
    SafetyDecisionOut,
    SafetyItemOut,
    SafetyMatchOut,
    SafetyPageOut,
)

router = Router(tags=["review"], auth=staff_auth)

REVIEW = Page.SafetyStatus.REVIEW
BLOCKED = Page.SafetyStatus.BLOCKED


def safety_item(page: Page) -> SafetyItemOut:
    raw_matches = page.safety_matches if isinstance(page.safety_matches, list) else []
    matches = [
        SafetyMatchOut(
            term=str(match.get("term", "")),
            category=str(match.get("category", "")),
            action=str(match.get("action", "")),
        )
        for match in raw_matches
        if isinstance(match, dict)
    ]
    tags = page.tags if isinstance(page.tags, list) else []
    return SafetyItemOut(
        id=page.pk,
        site=page.site.domain,
        title=page.title,
        url=page.url,
        tags=[tag for tag in tags if isinstance(tag, str)],
        matches=matches,
        categories=sorted({match.category for match in matches if match.category}),
        flagged_at=page.safety_flagged_at,
    )


@router.get("", response=SafetyPageOut)
def safety_queue(request: HttpRequest, cursor: int | None = None, limit: int = 50) -> SafetyPageOut:
    """Pages waiting for a safety decision, by id; `cursor` is the last page id seen."""
    limit = clamp_limit(limit, 50)
    queryset = Page.objects.filter(safety_status=REVIEW).select_related("site")
    if cursor is not None:
        queryset = queryset.filter(pk__gt=cursor)
    pages = list(queryset.order_by("pk")[: limit + 1])
    page = pages[:limit]
    return SafetyPageOut(
        items=[safety_item(p) for p in page],
        next_cursor=page[-1].pk if len(pages) > limit else None,
    )


def _review_pages(ids: list[int]) -> tuple[list[Page], ErrorOut | ConflictOut | None]:
    pages = list(Page.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    missing = sorted(set(ids) - {p.pk for p in pages})
    if missing:
        return pages, ErrorOut(detail=f"Unknown page ids: {', '.join(map(str, missing))}")
    conflicts = [
        ConflictItemOut(kind="page", id=p.pk, status=p.safety_status, reason="not in safety review")
        for p in pages
        if p.safety_status != REVIEW
    ]
    if conflicts:
        return pages, ConflictOut(
            detail="Only pages in review can be decided here", conflicts=conflicts
        )
    return pages, None


SAFETY_RESPONSES = {200: SafetyDecisionOut, 404: ErrorOut, 409: ConflictOut, 422: ErrorOut}


@router.post("/clear", response=SAFETY_RESPONSES)
def clear(
    request: HttpRequest, payload: IdsIn
) -> Status[SafetyDecisionOut | ErrorOut | ConflictOut]:
    """Mark review pages clear and release their media (same service as the admin action)."""
    with transaction.atomic():
        pages, error = _review_pages(payload.ids)
        if error is not None:
            return Status(404 if isinstance(error, ErrorOut) else 409, error)
        for page in pages:
            set_safety_status(page, Page.SafetyStatus.CLEAR, by=request.user.get_username())
            # The reviewer answered the matched terms; the score decides whether this page's
            # media may be fetched (ADR 0018). Pages indexed before the gate have no score yet.
            rescore_page(page)
    return Status(200, SafetyDecisionOut(status="clear", page_ids=[p.pk for p in pages]))


@router.post("/block", response=SAFETY_RESPONSES)
def block(
    request: HttpRequest, payload: BlockIn
) -> Status[SafetyDecisionOut | ErrorOut | ConflictOut]:
    """Block review pages with a required reason; cancels media and deletes frame hashes."""
    reason = payload.reason.strip()
    if not reason:
        return Status(422, ErrorOut(detail="A reason is required to block a page"))
    with transaction.atomic():
        pages, error = _review_pages(payload.ids)
        if error is not None:
            return Status(404 if isinstance(error, ErrorOut) else 409, error)
        for page in pages:
            set_safety_status(
                page, Page.SafetyStatus.BLOCKED, note=reason, by=request.user.get_username()
            )
    return Status(200, SafetyDecisionOut(status="blocked", page_ids=[p.pk for p in pages]))


def review_count() -> int:
    return Page.objects.filter(safety_status=REVIEW).count()


def blocked_count() -> int:
    """Total pages blocked for safety (issue #40). Informational only: nothing to decide here,
    the incident procedure (docs/safety.md) is per page in the admin."""
    return Page.objects.filter(safety_status=BLOCKED).count()
