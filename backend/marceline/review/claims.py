"""Staff queue for refused customer upload claims (issue #29 amendment, operator 2026-09-17).

`marceline/app/fingerprints.py` answers every refused claim — another organisation's file, an
unowned existing file, or hashes that largely duplicate another original — with one neutral 409
(`claim_needs_review`) that names nobody: deciding who holds the rights is not something a server
can do (ADR 0019), and the customer must never learn which case applied or about whom. The reason,
the organisation and the original(s) touched go only here, to `marceline.RefusedClaim`, for a
human to work through. Nothing in this module is reachable from the customer API.
"""

from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status

from marceline.models import RefusedClaim
from marceline.review.auth import staff_auth
from marceline.review.common import MAX_LIMIT
from marceline.review.schemas import (
    ClaimConflictOut,
    ClaimDecisionIn,
    ClaimItemOut,
    ClaimListOut,
    ClaimReferenceOut,
    ClaimStatusFilter,
    ErrorOut,
)
from matching.models import Reference

router = Router(tags=["review"], auth=staff_auth)

ClaimState = RefusedClaim.Status
STATUS_FILTERS: dict[str, tuple[str, ...]] = {
    "open": (ClaimState.OPEN,),
    "resolved": (ClaimState.RESOLVED,),
    "dismissed": (ClaimState.DISMISSED,),
    "all": tuple(ClaimState.values),
}
NOT_FOUND = ErrorOut(detail="Unknown claim")


def _references(reference_ids: list[int]) -> list[ClaimReferenceOut]:
    if not reference_ids:
        return []
    rows = Reference.objects.filter(pk__in=reference_ids).select_related("creator")
    by_id = {row.pk: row for row in rows}
    return [
        ClaimReferenceOut(id=rid, title=row.title, creator_name=row.creator.name)
        for rid in reference_ids
        if (row := by_id.get(rid)) is not None
    ]


def _item(claim: RefusedClaim) -> ClaimItemOut:
    return ClaimItemOut(
        id=claim.pk,
        organisation_id=claim.organisation_id,
        organisation_name=claim.organisation.name,
        sha256=claim.sha256,
        reason=claim.reason,
        overlap_share=claim.overlap_share,
        references=_references(list(claim.reference_ids or [])),
        requested_by=_username(claim.requested_by),
        status=claim.status,
        note=claim.note,
        created_at=claim.created_at,
        resolved_at=claim.resolved_at,
        resolved_by=_username(claim.resolved_by),
    )


def _username(user: User | None) -> str | None:
    return user.get_username() if user is not None else None


def open_count() -> int:
    """For the navigation badge (`GET /review/counts`)."""
    return RefusedClaim.objects.filter(status=ClaimState.OPEN).count()


@router.get("", response={200: ClaimListOut, 422: ErrorOut})
def claim_queue(
    request: HttpRequest, status: ClaimStatusFilter = "open", limit: int = MAX_LIMIT
) -> Status[ClaimListOut | ErrorOut]:
    """Refused claims, open ones first by default, newest first.

    `status`: open|resolved|dismissed|all.
    """
    statuses = STATUS_FILTERS.get(status)
    if statuses is None:
        return Status(422, ErrorOut(detail=f"Unknown status filter: {status}"))
    limit = MAX_LIMIT if limit < 1 else min(limit, MAX_LIMIT)
    queryset = RefusedClaim.objects.filter(status__in=statuses).select_related(
        "organisation", "requested_by", "resolved_by"
    )
    rows = list(queryset.order_by("-created_at", "-pk")[: limit + 1])
    page = rows[:limit]
    return Status(200, ClaimListOut(items=[_item(c) for c in page], truncated=len(rows) > limit))


def _decide(
    request: HttpRequest, claim_id: int, payload: ClaimDecisionIn, *, target: str
) -> Status[ClaimItemOut | ErrorOut | ClaimConflictOut]:
    claim = RefusedClaim.objects.select_related("organisation").filter(pk=claim_id).first()
    if claim is None:
        return Status(404, NOT_FOUND)
    if claim.status != ClaimState.OPEN:
        return Status(
            409,
            ClaimConflictOut(
                detail=f"Claim {claim.pk} is already {claim.status}; reopen it in the admin first."
            ),
        )
    user = request.user if request.user.is_authenticated else None
    with transaction.atomic():
        claim.status = target
        claim.note = payload.note
        claim.resolved_at = timezone.now()
        claim.resolved_by = user
        claim.save(update_fields=["status", "note", "resolved_at", "resolved_by"])
    return Status(200, _item(claim))


@router.post(
    "/{claim_id}/resolve", response={200: ClaimItemOut, 404: ErrorOut, 409: ClaimConflictOut}
)
def resolve(
    request: HttpRequest, claim_id: int, payload: ClaimDecisionIn
) -> Status[ClaimItemOut | ErrorOut | ClaimConflictOut]:
    """Staff acted on it (e.g. rights sorted out, or the customer was told to use another file)."""
    return _decide(request, claim_id, payload, target=ClaimState.RESOLVED)


@router.post(
    "/{claim_id}/dismiss", response={200: ClaimItemOut, 404: ErrorOut, 409: ClaimConflictOut}
)
def dismiss(
    request: HttpRequest, claim_id: int, payload: ClaimDecisionIn
) -> Status[ClaimItemOut | ErrorOut | ClaimConflictOut]:
    """No follow-up needed (e.g. a false alarm from a hash-overlap threshold)."""
    return _decide(request, claim_id, payload, target=ClaimState.DISMISSED)
