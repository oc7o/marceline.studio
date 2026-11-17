"""Source discovery review (ADR 0013): `crawler.SourceCandidate` rows and the search call log.

Evidence is returned as plain strings; the SPA renders it as text and never loads anything from a
candidate domain. All decisions call the Django admin actions, so the rules (approval creates a
disabled Site if absent; undoing it deletes only that Site while it is disabled and holds no crawl
data; Sites that existed before are never modified) live in one place.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from django.contrib import admin
from django.db import transaction
from django.db.models import Count, QuerySet, Sum
from django.http import HttpRequest
from django.utils import timezone
from ninja import Field, Router, Schema, Status

from catalog.models import Site
from crawler.admin import SourceCandidateAdmin
from crawler.discovery.services import undo_approval_blocker
from crawler.models import SearchCall, SourceCandidate
from marceline.review.auth import staff_auth
from marceline.review.schemas import MAX_BULK_IDS, ConflictItemOut, ConflictOut, ErrorOut

router = Router(tags=["review"], auth=staff_auth)

MAX_CANDIDATES = 500
MAX_SEARCH_CALLS = 100
TEXT_LIMIT = 2000

CandidateStatusFilter = Literal["proposed", "approved", "rejected", "hoster", "ignored", "all"]
CandidateAction = Literal["approve", "reject", "hoster", "ignore", "reset"]

ACTION_STATUS: dict[str, str] = {
    "approve": SourceCandidate.Status.APPROVED,
    "reject": SourceCandidate.Status.REJECTED,
    "hoster": SourceCandidate.Status.HOSTER,
    "ignore": SourceCandidate.Status.IGNORED,
    "reset": SourceCandidate.Status.PROPOSED,
}


class QuerySampleOut(Schema):
    query: str
    rank: int | None
    url: str = Field(description="Result URL as text; never fetched or linked by the UI.")
    title: str


class EvidenceOut(Schema):
    queries: list[QuerySampleOut]
    linking_pages: list[str]
    redirects_to: str | None
    mirror_of: str | None


class CandidateOut(Schema):
    id: int
    domain: str
    status: str
    kind: str
    cms_guess: str
    has_search: bool
    video_links_seen: int
    robots_allows: bool | None
    http_status: int | None
    blocked_reason: str
    title: str
    search_hits: int
    distinct_creators: int
    best_rank: int | None
    outlink_pages: int
    first_seen_at: datetime
    last_seen_at: datetime
    probed_at: datetime | None
    evidence: EvidenceOut
    notes: str


class CandidateListOut(Schema):
    items: list[CandidateOut]
    total: int = Field(description="Rows matching the filters (items are capped at `limit`).")
    status_counts: dict[str, int]
    cms_values: list[str]


class CandidateDecisionIn(Schema):
    ids: list[int] = Field(min_length=1, max_length=MAX_BULK_IDS)
    action: CandidateAction


class CandidateDecisionOut(Schema):
    action: CandidateAction
    status: str
    candidate_ids: list[int]
    created_site_ids: list[int] = Field(description="Disabled Sites created by an approval.")
    removed_site_ids: list[int] = Field(
        description="Untouched disabled Sites deleted because their approval was undone."
    )


class SearchCallOut(Schema):
    id: int
    created_at: datetime
    engine: str
    mode: str
    query: str
    status: str
    result_count: int
    cost_usd: float
    error: str


class SearchCallsOut(Schema):
    total_cost_usd: float
    cost_24h_usd: float
    total_calls: int
    calls: list[SearchCallOut]


def _text(value: object, limit: int = TEXT_LIMIT) -> str:
    return value[:limit] if isinstance(value, str) else ""


def evidence_out(evidence: object) -> EvidenceOut:
    data = evidence if isinstance(evidence, dict) else {}
    raw_queries = data.get("queries")
    raw_pages = data.get("linking_pages")
    queries = [
        QuerySampleOut(
            query=_text(sample.get("query")),
            rank=sample["rank"] if isinstance(sample.get("rank"), int) else None,
            url=_text(sample.get("url")),
            title=_text(sample.get("title")),
        )
        for sample in (raw_queries if isinstance(raw_queries, list) else [])
        if isinstance(sample, dict)
    ]
    pages = [_text(url) for url in (raw_pages if isinstance(raw_pages, list) else [])]
    return EvidenceOut(
        queries=queries,
        linking_pages=[url for url in pages if url],
        redirects_to=_text(data.get("redirects_to")) or None,
        mirror_of=_text(data.get("mirror_of")) or None,
    )


def candidate_out(candidate: SourceCandidate) -> CandidateOut:
    return CandidateOut(
        id=candidate.pk,
        domain=candidate.domain,
        status=candidate.status,
        kind=candidate.kind,
        cms_guess=candidate.cms_guess,
        has_search=candidate.has_search,
        video_links_seen=candidate.video_links_seen,
        robots_allows=candidate.robots_allows,
        http_status=candidate.http_status,
        blocked_reason=candidate.blocked_reason,
        title=candidate.title,
        search_hits=candidate.search_hits,
        distinct_creators=candidate.distinct_creators,
        best_rank=candidate.best_rank,
        outlink_pages=candidate.outlink_pages,
        first_seen_at=candidate.first_seen_at,
        last_seen_at=candidate.last_seen_at,
        probed_at=candidate.probed_at,
        evidence=evidence_out(candidate.evidence),
        notes=candidate.notes,
    )


@router.get("/candidates", response=CandidateListOut)
def candidates(
    request: HttpRequest,
    status: CandidateStatusFilter = "proposed",
    kind: str | None = None,
    cms_guess: str | None = None,
    blocked: bool | None = None,
    limit: int = MAX_CANDIDATES,
) -> CandidateListOut:
    """Candidates by distinct creators, then search hits (both descending), then domain."""
    queryset = SourceCandidate.objects.all()
    if status != "all":
        queryset = queryset.filter(status=status)
    if kind:
        queryset = queryset.filter(kind=kind)
    if cms_guess:
        queryset = queryset.filter(cms_guess=cms_guess)
    if blocked is not None:
        queryset = (
            queryset.exclude(blocked_reason="") if blocked else queryset.filter(blocked_reason="")
        )
    limit = MAX_CANDIDATES if limit < 1 else min(limit, MAX_CANDIDATES)
    rows = queryset.order_by("-distinct_creators", "-search_hits", "domain")[:limit]
    counts = dict(
        SourceCandidate.objects.values("status")
        .annotate(total=Count("pk"))
        .values_list("status", "total")
        .order_by()
    )
    return CandidateListOut(
        items=[candidate_out(candidate) for candidate in rows],
        total=queryset.count(),
        status_counts={value: int(counts.get(value, 0)) for value in SourceCandidate.Status},
        cms_values=sorted(
            SourceCandidate.objects.values_list("cms_guess", flat=True).distinct().order_by()
        ),
    )


def _model_admin() -> SourceCandidateAdmin:
    model_admin = admin.site.get_model_admin(SourceCandidate)
    if not isinstance(model_admin, SourceCandidateAdmin):
        raise RuntimeError("SourceCandidate admin is not registered")
    return model_admin


def apply_action(
    request: HttpRequest, action: CandidateAction, queryset: QuerySet[SourceCandidate]
) -> None:
    """The admin's actions."""
    model_admin = _model_admin()
    if action == "approve":
        model_admin.approve(request, queryset)
    elif action == "reject":
        model_admin.reject(request, queryset)
    elif action == "hoster":
        model_admin.mark_hoster(request, queryset)
    elif action == "ignore":
        model_admin.ignore(request, queryset)
    else:
        model_admin.reset(request, queryset)


DECISION_RESPONSES = {200: CandidateDecisionOut, 404: ErrorOut, 409: ConflictOut}


@router.post("/candidates/decide", response=DECISION_RESPONSES)
def decide(
    request: HttpRequest, payload: CandidateDecisionIn
) -> Status[CandidateDecisionOut | ErrorOut | ConflictOut]:
    """Approve (creates a disabled Site if absent), reject, mark hoster, ignore or reset.

    Moving an approved candidate elsewhere undoes the approval: the disabled Site it created is
    deleted. If that Site is enabled or already has crawl data, the request is refused (409) and
    the Site is changed in the admin first."""
    ids = sorted(set(payload.ids))
    target = ACTION_STATUS[payload.action]
    with transaction.atomic():
        rows = list(SourceCandidate.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
        missing = sorted(set(ids) - {row.pk for row in rows})
        if missing:
            return Status(
                404, ErrorOut(detail=f"Unknown candidate ids: {', '.join(map(str, missing))}")
            )
        conflicts = [
            ConflictItemOut(kind="candidate", id=row.pk, status=row.status, reason=blocker)
            for row in rows
            if row.status == SourceCandidate.Status.APPROVED
            and target != row.status
            and (blocker := undo_approval_blocker(row))
        ]
        if conflicts:
            return Status(
                409,
                ConflictOut(
                    detail="Approval can't be undone: disable the Site or remove its data in the "
                    "admin first",
                    conflicts=conflicts,
                ),
            )
        domains = [row.domain for row in rows]
        before = set(Site.objects.filter(domain__in=domains).values_list("pk", flat=True))
        apply_action(request, payload.action, SourceCandidate.objects.filter(pk__in=ids))
        after = set(Site.objects.filter(domain__in=domains).values_list("pk", flat=True))
    return Status(
        200,
        CandidateDecisionOut(
            action=payload.action,
            status=target,
            candidate_ids=ids,
            created_site_ids=sorted(after - before),
            removed_site_ids=sorted(before - after),
        ),
    )


@router.get("/search-calls", response=SearchCallsOut)
def search_calls(request: HttpRequest, limit: int = 25) -> SearchCallsOut:
    """Search provider spend and the latest calls (read-only audit log)."""
    limit = 25 if limit < 1 else min(limit, MAX_SEARCH_CALLS)
    since = timezone.now() - timedelta(hours=24)
    totals = SearchCall.objects.aggregate(total=Sum("cost_usd"))
    recent = SearchCall.objects.filter(created_at__gte=since).aggregate(total=Sum("cost_usd"))
    return SearchCallsOut(
        total_cost_usd=float(totals["total"] or Decimal(0)),
        cost_24h_usd=float(recent["total"] or Decimal(0)),
        total_calls=SearchCall.objects.count(),
        calls=[
            SearchCallOut(
                id=call.pk,
                created_at=call.created_at,
                engine=call.engine,
                mode=call.mode,
                query=call.query,
                status=call.status,
                result_count=call.result_count,
                cost_usd=float(call.cost_usd),
                error=_text(call.error, 500),
            )
            for call in SearchCall.objects.order_by("-created_at", "-pk")[:limit]
        ],
    )
