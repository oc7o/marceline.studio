"""Staff safety incidents (issue #66): the list, the hotline report draft, and "mark reported".

Text only, like the rest of the safety screen: no media fields and no media ids, ever. Page URLs
appear in exactly one response — the report draft — because the draft *is* the report a human
copies into the hotline's form (docs/safety.md). Nothing here fetches a page or submits a report.
"""

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status

from index import incident_report
from index.incidents import ACTIVE_STATUSES, mark_reported
from index.models import SafetyIncident
from marceline.review.auth import staff_auth
from marceline.review.common import MAX_LIMIT
from marceline.review.schemas import (
    ConflictItemOut,
    ConflictOut,
    ErrorOut,
    IncidentItemOut,
    IncidentListOut,
    IncidentReportedIn,
    IncidentTermOut,
    ReportDraftOut,
    ReportDraftTextOut,
    ReportUrlOut,
)

router = Router(tags=["review"], auth=staff_auth)

IncidentStatus = SafetyIncident.Status
STATUS_FILTERS: dict[str, tuple[str, ...]] = {
    "active": ACTIVE_STATUSES,
    "open": (IncidentStatus.OPEN,),
    "reported": (IncidentStatus.REPORTED,),
    "settled": (IncidentStatus.CLOSED, IncidentStatus.FALSE_POSITIVE),
    "all": tuple(IncidentStatus.values),
}
NOT_FOUND = ErrorOut(detail="Unknown incident")


def _item(incident: SafetyIncident) -> IncidentItemOut:
    terms = incident.terms if isinstance(incident.terms, list) else []
    rows = list(incident.incident_pages.all())
    return IncidentItemOut(
        id=incident.pk,
        reference_id=incident_report.reference_id(incident.pk),
        status=incident.status,
        categories=list(incident_report.categories(incident)),
        terms=[
            IncidentTermOut(category=str(term.get("category", "")), term=str(term.get("term", "")))
            for term in terms
            if isinstance(term, dict)
        ],
        detected_at=incident.detected_at,
        last_seen_at=incident.last_seen_at,
        reported_at=incident.reported_at,
        reported_via=incident.reported_via,
        reference=incident.reference,
        report_category=incident.report_category,
        closed_at=incident.closed_at,
        url_count=len(rows),
        sites=sorted({row.site_domain for row in rows if row.site_domain}),
    )


@router.get("", response={200: IncidentListOut, 422: ErrorOut})
def incident_list(
    request: HttpRequest, status: str = "active", limit: int = MAX_LIMIT
) -> Status[IncidentListOut | ErrorOut]:
    """Safety cases, open ones first, newest first. `status`: active|open|reported|settled|all."""
    statuses = STATUS_FILTERS.get(status)
    if statuses is None:
        return Status(422, ErrorOut(detail=f"Unknown status filter: {status}"))
    limit = MAX_LIMIT if limit < 1 else min(limit, MAX_LIMIT)
    queryset = SafetyIncident.objects.filter(status__in=statuses).prefetch_related("incident_pages")
    ordered = sorted(
        queryset, key=lambda i: (i.status != IncidentStatus.OPEN, -i.detected_at.timestamp(), -i.pk)
    )
    return Status(
        200,
        IncidentListOut(items=[_item(i) for i in ordered[:limit]], truncated=len(ordered) > limit),
    )


def _conflict(incident: SafetyIncident, reason: str, detail: str) -> ConflictOut:
    return ConflictOut(
        detail=detail,
        conflicts=[
            ConflictItemOut(kind="incident", id=incident.pk, status=incident.status, reason=reason)
        ],
    )


@router.get(
    "/{incident_id}/report-draft", response={200: ReportDraftOut, 404: ErrorOut, 409: ConflictOut}
)
def report_draft(
    request: HttpRequest, incident_id: int
) -> Status[ReportDraftOut | ErrorOut | ConflictOut]:
    """Plain-text hotline report in German and English, for a human to copy into the form.

    Only for a running case: an open one (the first report) or a reported one (a supplement, e.g.
    for a mirror URL found after the report). A settled case gets 409 — it must not be reported.
    """
    incident = SafetyIncident.objects.filter(pk=incident_id).first()
    if incident is None:
        return Status(404, NOT_FOUND)
    if incident.status not in incident_report.DRAFTABLE_STATUSES:
        return Status(409, _conflict(incident, "case is settled", "Settled cases are not reported"))
    facts = incident_report.incident_facts(incident, reply_to=settings.SAFETY_REPORT_REPLY_TO)
    if not facts.urls:
        return Status(409, _conflict(incident, "no URL recorded", "This case has no URL to report"))
    return Status(
        200,
        ReportDraftOut(
            incident_id=incident.pk,
            reference_id=facts.reference_id,
            status=incident.status,
            template_version=incident_report.TEMPLATE_VERSION,
            channel=incident_report.REPORT_CHANNEL,
            supplement=facts.reported_at is not None,
            reported_at=facts.reported_at,
            reference=facts.hotline_reference,
            media_fetch=facts.media_fetch.value,
            urls=[
                ReportUrlOut(
                    url=item.url,
                    site=item.site,
                    evidence=item.evidence,
                    primary=item.primary,
                    first_seen_at=item.first_seen_at,
                    last_seen_at=item.last_seen_at,
                    new_since_report=item.new_since_report,
                )
                for item in facts.urls
            ],
            drafts=[
                ReportDraftTextOut(language=draft.language, subject=draft.subject, body=draft.body)
                for draft in (
                    incident_report.render(facts, language)
                    for language in incident_report.LANGUAGES
                )
            ],
        ),
    )


@router.post(
    "/{incident_id}/reported",
    response={200: IncidentItemOut, 404: ErrorOut, 409: ConflictOut, 422: ErrorOut},
)
def reported(
    request: HttpRequest, incident_id: int, payload: IncidentReportedIn
) -> Status[IncidentItemOut | ErrorOut | ConflictOut]:
    """A human filed the report: record when, where and the hotline's reference (blank = none
    was issued). Only an open case; corrections to a reported one go through the admin."""
    now = timezone.now()
    if payload.reported_at is not None and payload.reported_at > now:
        return Status(422, ErrorOut(detail="The report date cannot be in the future"))
    with transaction.atomic():
        incident = SafetyIncident.objects.select_for_update().filter(pk=incident_id).first()
        if incident is None:
            return Status(404, NOT_FOUND)
        if incident.status != IncidentStatus.OPEN:
            return Status(
                409,
                _conflict(incident, "not open", "Only an open case can be marked reported here"),
            )
        reference = payload.reference.strip()
        language = f", {payload.language}" if payload.language else ""
        note = (
            f"Marked reported in the staff safety screen (report draft "
            f"{incident_report.TEMPLATE_VERSION}{language})."
        )
        if not reference:
            note += " No reference number was issued."
        mark_reported(
            incident,
            reference=reference,
            via=payload.via.strip() or incident_report.REPORT_CHANNEL,
            report_category=payload.report_category.strip(),
            by=request.user.get_username(),
            note=note,
            reported_at=payload.reported_at or now,
        )
    incident = SafetyIncident.objects.prefetch_related("incident_pages").get(pk=incident_id)
    return Status(200, _item(incident))
