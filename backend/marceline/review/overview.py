"""Operations overview (the SPA start page): crawl and media throughput per enabled site, scheduler
state, open review work and the M2 numbers. Read-only; counts only, no media and no URLs."""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from django.db.models import Count, F, Q
from django.http import HttpRequest
from django.utils import timezone
from ninja import Field, Router, Schema

from catalog.models import Site
from catalog.request_accounting import origin_usage
from core.models import JobRun
from crawler.models import CrawlTask, SourceCandidate
from index.models import Media, Page
from marceline.review import clusters, references, safety
from marceline.review.auth import staff_auth
from marceline.review.m2_summary import M2Result, m2_summary
from marceline.review.resources import ResourcesOut, resources_snapshot
from matching.models import ReferenceLeak, ReferenceMatch

router = Router(tags=["review"], auth=staff_auth)

RECENT = timedelta(hours=1)
NEW_PAGES = timedelta(hours=24)


class AvailabilityOut(Schema):
    playable: int
    removed: int
    unknown: int


class SiteStatsOut(Schema):
    id: int
    domain: str
    name: str
    pages_total: int
    pages_new_24h: int
    tasks_pending: int
    tasks_done_1h: int
    tasks_failed_1h: int
    media_pending: int
    media_done_1h: int
    media_skipped: int
    availability: AvailabilityOut
    requests_24h: int = Field(
        description="Real page and robots.txt requests to the site's origin hosts in the rolling "
        "24 h budget window, retries and redirect hops included: what `daily_request_budget` "
        "limits. Media has its own budgets and is not included."
    )
    daily_request_budget: int


class JobOut(Schema):
    name: str
    last_status: str
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_succeeded_at: datetime | None
    summary: str


class ReviewWorkOut(Schema):
    reference_pairs: int = Field(description="Proposed (reference, page) pairs, as in the queue.")
    reference_leaks: int = Field(description="Proposed ReferenceLeak rows.")
    reference_matches: int = Field(description="Proposed ReferenceMatch rows.")
    clusters: int = Field(description="Clusters with proposed members.")
    safety: int = Field(description="Pages in safety review.")
    safety_blocked: int = Field(
        description="Pages blocked for safety (issue #40; informational, not actionable here)."
    )
    source_candidates: int = Field(description="Proposed source candidates.")


class M2Out(Schema):
    confirmed: int
    found_strong: int
    found_weak: int
    found_linked: int
    recall_strong: float | None
    recall_strong_weak: float | None
    recall_strong_weak_linked: float | None
    false_matches: int
    reviewed_matches: int
    false_match_rate: float | None
    unreviewed_suggestions: int
    unreviewed_matches: int
    passed: bool
    too_few_to_judge: bool
    recall_target: float
    false_match_limit: float
    max_distance: int


class OverviewOut(Schema):
    generated_at: datetime
    sites: list[SiteStatsOut]
    jobs: list[JobOut]
    review: ReviewWorkOut
    m2: M2Out
    resources: ResourcesOut


def m2_out(result: M2Result) -> M2Out:
    total = result.total
    return M2Out(
        confirmed=total.confirmed,
        found_strong=total.strong,
        found_weak=total.weak,
        found_linked=total.linked,
        recall_strong=total.recall(total.strong),
        recall_strong_weak=total.recall(total.strong + total.weak),
        recall_strong_weak_linked=total.recall(total.strong + total.weak + total.linked),
        false_matches=total.false,
        reviewed_matches=total.reviewed,
        false_match_rate=total.false_match_rate,
        unreviewed_suggestions=total.suggestions,
        unreviewed_matches=total.matches,
        passed=result.passed,
        too_few_to_judge=result.too_few_to_judge,
        recall_target=result.recall_target,
        false_match_limit=result.false_match_limit,
        max_distance=result.max_distance,
    )


def _by_site(rows: Iterable[Mapping[str, object]]) -> dict[int, dict[str, int]]:
    """Grouped `values()` rows keyed by site id, counts only."""
    result: dict[int, dict[str, int]] = {}
    for row in rows:
        site_id = row["site_id"]
        if isinstance(site_id, int):
            result[site_id] = {key: value for key, value in row.items() if isinstance(value, int)}
    return result


def site_stats(now: datetime) -> list[SiteStatsOut]:
    sites = list(Site.objects.filter(enabled=True).order_by("domain"))
    ids = [site.pk for site in sites]
    recent, new_since = now - RECENT, now - NEW_PAGES
    requests = origin_usage(ids, now)
    pages = _by_site(
        list(
            Page.objects.filter(site_id__in=ids)
            .values("site_id")
            .annotate(
                total=Count("pk"),
                new=Count("pk", filter=Q(first_seen__gte=new_since)),
                playable=Count("pk", filter=Q(availability=Page.Availability.PLAYABLE)),
                removed=Count("pk", filter=Q(availability=Page.Availability.REMOVED)),
                unknown=Count("pk", filter=Q(availability=Page.Availability.UNKNOWN)),
            )
        )
    )
    done, failed = CrawlTask.Status.DONE, CrawlTask.Status.FAILED
    tasks = _by_site(
        list(
            CrawlTask.objects.filter(site_id__in=ids)
            .values("site_id")
            .annotate(
                pending=Count("pk", filter=Q(status=CrawlTask.Status.PENDING)),
                done=Count("pk", filter=Q(status=done, updated_at__gte=recent)),
                failed=Count("pk", filter=Q(status=failed, updated_at__gte=recent)),
            )
        )
    )
    media = _by_site(
        list(
            Media.objects.filter(page__site_id__in=ids)
            .values(site_id=F("page__site_id"))
            .annotate(
                pending=Count("pk", filter=Q(status=Media.Status.PENDING)),
                done=Count("pk", filter=Q(status=Media.Status.DONE, fetched_at__gte=recent)),
                # Robots-blocked media is skipped too, for a reason of its own (ADR 0007).
                skipped=Count(
                    "pk", filter=Q(status__in=(Media.Status.SKIPPED, Media.Status.ROBOTS_BLOCKED))
                ),
            )
        )
    )
    empty: dict[str, int] = {}
    result = []
    for site in sites:
        page_row = pages.get(site.pk, empty)
        task_row = tasks.get(site.pk, empty)
        media_row = media.get(site.pk, empty)
        result.append(
            SiteStatsOut(
                id=site.pk,
                domain=site.domain,
                name=site.name,
                pages_total=page_row.get("total", 0),
                pages_new_24h=page_row.get("new", 0),
                tasks_pending=task_row.get("pending", 0),
                tasks_done_1h=task_row.get("done", 0),
                tasks_failed_1h=task_row.get("failed", 0),
                media_pending=media_row.get("pending", 0),
                media_done_1h=media_row.get("done", 0),
                media_skipped=media_row.get("skipped", 0),
                availability=AvailabilityOut(
                    playable=page_row.get("playable", 0),
                    removed=page_row.get("removed", 0),
                    unknown=page_row.get("unknown", 0),
                ),
                requests_24h=requests.get(site.pk, 0),
                daily_request_budget=site.daily_request_budget,
            )
        )
    return result


def jobs() -> list[JobOut]:
    return [
        JobOut(
            name=job.name,
            last_status=job.last_status,
            last_started_at=job.last_started_at,
            last_finished_at=job.last_finished_at,
            last_succeeded_at=job.last_succeeded_at,
            summary=job.last_message,
        )
        for job in JobRun.objects.order_by("name")
    ]


def review_work() -> ReviewWorkOut:
    return ReviewWorkOut(
        reference_pairs=references.proposed_pair_count(),
        reference_leaks=ReferenceLeak.objects.filter(status=ReferenceLeak.Status.PROPOSED).count(),
        reference_matches=ReferenceMatch.objects.filter(
            status=ReferenceMatch.Status.PROPOSED
        ).count(),
        clusters=clusters.proposed_cluster_count(),
        safety=safety.review_count(),
        safety_blocked=safety.blocked_count(),
        source_candidates=SourceCandidate.objects.filter(
            status=SourceCandidate.Status.PROPOSED
        ).count(),
    )


@router.get("", response=OverviewOut)
def overview(request: HttpRequest) -> OverviewOut:
    """Everything on the start page in one request (the SPA refreshes it every 60 s)."""
    now = timezone.now()
    return OverviewOut(
        generated_at=now,
        sites=site_stats(now),
        jobs=jobs(),
        review=review_work(),
        m2=m2_out(m2_summary()),
        resources=resources_snapshot(now),
    )
