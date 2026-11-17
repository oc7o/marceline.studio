"""Customer release queue: blocked "flagged term" pages crediting a customer creator (safety.md).

**Text only, always.** These pages are `blocked`: the responses carry title, tags, performer
credits, site, matched rule terms and which customer creator matched — no media ids, no image
passthrough, not even the page URL. Staff may release a page for fingerprinting (a logged human
release, re-checked against the current text and rules; any other safety hit keeps it blocked) or
keep it blocked. `marceline/safety_rules.py` owns both decisions.
"""

from collections.abc import Mapping

from django.http import HttpRequest
from ninja import Router, Status

from index.models import Page
from marceline.review.auth import staff_auth
from marceline.review.common import clamp_limit
from marceline.review.schemas import (
    ConflictItemOut,
    ConflictOut,
    CustomerCreditOut,
    ErrorOut,
    KeepBlockedIn,
    ReleaseIn,
    SafetyDecisionOut,
    SafetyMatchOut,
    SafetyReleaseItemOut,
    SafetyReleasePageOut,
)
from marceline.safety_rules import (
    ReleaseRefused,
    customer_creators,
    customer_credits,
    keep_blocked,
    release_pages,
)

router = Router(tags=["review"], auth=staff_auth)


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def release_item(page: Page, credits: list[CustomerCreditOut]) -> SafetyReleaseItemOut:
    raw = page.raw_extraction if isinstance(page.raw_extraction, Mapping) else {}
    matches = [
        SafetyMatchOut(
            term=str(match.get("term", "")),
            category=str(match.get("category", "")),
            action=str(match.get("action", "")),
        )
        for match in (page.safety_matches if isinstance(page.safety_matches, list) else [])
        if isinstance(match, dict)
    ]
    return SafetyReleaseItemOut(
        id=page.pk,
        site=page.site.domain,
        title=page.title,
        tags=_strings(page.tags),
        performers=_strings(raw.get("performers")),
        matches=matches,
        customer_credits=credits,
        flagged_at=page.safety_flagged_at,
        queued_at=page.safety_release_at,
    )


@router.get("", response=SafetyReleasePageOut)
def release_queue(
    request: HttpRequest, cursor: int | None = None, limit: int = 50
) -> SafetyReleasePageOut:
    """Queued pages by id; `cursor` is the last page id seen. Credits are computed now."""
    limit = clamp_limit(limit, 50)
    queryset = Page.objects.filter(
        safety_status=Page.SafetyStatus.BLOCKED, safety_release=Page.SafetyRelease.QUEUED
    ).select_related("site")
    if cursor is not None:
        queryset = queryset.filter(pk__gt=cursor)
    pages = list(queryset.order_by("pk")[: limit + 1])
    shown = pages[:limit]
    creators = customer_creators()
    items = [
        release_item(
            page,
            [
                CustomerCreditOut(
                    creator_id=credit.creator.pk,
                    creator_name=credit.creator.name,
                    matched_in=list(credit.matched_in),
                )
                for credit in customer_credits(page, creators)
            ],
        )
        for page in shown
    ]
    return SafetyReleasePageOut(
        items=items, next_cursor=shown[-1].pk if len(pages) > limit else None
    )


RESPONSES = {200: SafetyDecisionOut, 404: ErrorOut, 409: ConflictOut, 422: ErrorOut}


def _unknown(ids: list[int]) -> ErrorOut | None:
    known = set(Page.objects.filter(pk__in=ids).values_list("pk", flat=True))
    missing = sorted(set(ids) - known)
    return ErrorOut(detail=f"Unknown page ids: {', '.join(map(str, missing))}") if missing else None


def _refused(exc: ReleaseRefused) -> ConflictOut:
    status = Page.objects.filter(pk=exc.page_id).values_list("safety_status", flat=True).first()
    return ConflictOut(
        detail="Nothing was changed",
        conflicts=[
            ConflictItemOut(kind="page", id=exc.page_id, status=status or "", reason=exc.reason)
        ],
    )


@router.post("/release", response=RESPONSES)
def release(
    request: HttpRequest, payload: ReleaseIn
) -> Status[SafetyDecisionOut | ErrorOut | ConflictOut]:
    """Release queued pages for fingerprinting (reason required). All or nothing: a page with
    any other safety hit, or without a customer credit any more, refuses the whole request."""
    reason = payload.reason.strip()
    if not reason:
        return Status(422, ErrorOut(detail="A reason is required to release a page"))
    if (error := _unknown(payload.ids)) is not None:
        return Status(404, error)
    try:
        pages = release_pages(payload.ids, by=request.user.get_username(), note=reason)
    except ReleaseRefused as exc:
        return Status(409, _refused(exc))
    return Status(200, SafetyDecisionOut(status="released", page_ids=[p.pk for p in pages]))


@router.post("/keep-blocked", response=RESPONSES)
def keep(
    request: HttpRequest, payload: KeepBlockedIn
) -> Status[SafetyDecisionOut | ErrorOut | ConflictOut]:
    """Keep queued pages blocked. The decision stands on later crawls."""
    if (error := _unknown(payload.ids)) is not None:
        return Status(404, error)
    try:
        pages = keep_blocked(
            payload.ids, by=request.user.get_username(), note=payload.reason.strip()
        )
    except ReleaseRefused as exc:
        return Status(409, _refused(exc))
    return Status(200, SafetyDecisionOut(status="kept_blocked", page_ids=[p.pk for p in pages]))
