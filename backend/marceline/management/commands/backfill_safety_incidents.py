"""Turn hand-written safety notes into incident records (issue #66).

Before the incident model existed, a report about a blocked page was recorded by hand in the page's
`safety_note`. That hand-written record is what the incident model replaces, so it has to be
carried over rather than retyped: this command creates (or reuses) the page's incident, marks it
reported with the facts below, and keeps the original note **verbatim** underneath them so nothing
that was written can be lost in translation.

The constants below are sample values; set them to the facts of the reports being carried over:

- when and through which channel the report was made,
- the category selected in the form,
- **no reference number** (so `reference` stays blank — blank means none was issued),
- the reply address,
- the content was never opened and no media was ever fetched.

Idempotent: a second run changes nothing. A note a human has since edited is never overwritten —
the command reports it and moves on.
"""

import re
from datetime import UTC, datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from index.incident_report import REPORT_CHANNEL
from index.incidents import mark_reported, open_or_link
from index.models import Page, SafetyIncident
from marceline.safety_incidents import cluster_ref, related_pages

REPORTED_AT = datetime(2026, 1, 15, tzinfo=UTC)
REPORTED_VIA = REPORT_CHANNEL
REPORT_CATEGORY = "abuse material involving minors"
REPLY_TO = "reporting@marceline.studio"
DECIDED_BY = "operator"
MARKER = "Backfilled from the page's safety note (issue #66)."
#: A safety note that mentions one of these was a report record, not a review reason.
REPORT_MARKERS = re.compile(
    r"beschwerdestelle|inhope|ncmec|hotline|gemeldet|reported",
    re.IGNORECASE,
)


def incident_note(page_note: str) -> str:
    """The incident's note: the recorded facts, then the operator's own words verbatim."""
    return "\n".join(
        (
            MARKER,
            f"Reported {REPORTED_AT.date().isoformat()} via {REPORTED_VIA}.",
            f'Category selected in the form: "{REPORT_CATEGORY}".',
            "No reference number was issued by the hotline.",
            f"Reply possible to {REPLY_TO}.",
            "The content was never opened; no media was fetched, stored or attached.",
            "",
            "Original safety note, verbatim:",
            page_note.strip(),
        )
    )


class Command(BaseCommand):
    help = (
        "Backfill safety incidents from the hand-written safety notes of blocked pages (issue "
        "#66). Idempotent; run --dry-run first."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only blocked pages of this domain.")
        parser.add_argument(
            "--page",
            type=int,
            action="append",
            default=[],
            help="Backfill exactly this page id (repeatable); skips the note check.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = bool(options["dry_run"])
        pages = self._pages(options)
        reported = unchanged = edited = 0
        for page in pages:
            incident, created = self._incident(page, dry_run=dry_run)
            if incident is None:
                self.stdout.write(f"page={page.pk} would open an incident and mark it reported")
                reported += 1
                continue
            note = incident_note(page.safety_note)
            if incident.note and MARKER not in incident.note:
                self.stdout.write(
                    f"page={page.pk} incident={incident.pk} left alone: its note was written by "
                    "a human, not by this backfill"
                )
                edited += 1
                continue
            if (
                incident.status == SafetyIncident.Status.REPORTED
                and incident.note == note
                and incident.reported_at == REPORTED_AT
                and incident.reported_via == REPORTED_VIA
                and incident.report_category == REPORT_CATEGORY
                and incident.reference == ""
            ):
                unchanged += 1
                continue
            if dry_run:
                self.stdout.write(
                    f"page={page.pk} incident={incident.pk} would be marked reported "
                    f"{REPORTED_AT.date().isoformat()} via {REPORTED_VIA}, no reference number"
                )
                reported += 1
                continue
            incident.note = note
            mark_reported(
                incident,
                via=REPORTED_VIA,
                report_category=REPORT_CATEGORY,
                by=DECIDED_BY,
                reported_at=REPORTED_AT,
            )
            verb = "opened and marked" if created else "marked"
            self.stdout.write(
                f"page={page.pk} incident={incident.pk} {verb} reported "
                f"{REPORTED_AT.date().isoformat()}, no reference number issued"
            )
            reported += 1
        mode = " (dry run, nothing written)" if dry_run else ""
        self.stdout.write(
            f"{len(pages)} page(s) with a recorded report{mode}: {reported} backfilled, "
            f"{unchanged} already correct, {edited} left alone (human-edited note)"
        )

    def _pages(self, options: dict[str, Any]) -> list[Page]:
        queryset = Page.objects.filter(safety_status=Page.SafetyStatus.BLOCKED).select_related(
            "site"
        )
        if options["site"]:
            queryset = queryset.filter(site__domain=options["site"])
        if options["page"]:
            return list(queryset.filter(pk__in=options["page"]).order_by("pk"))
        return [
            page
            for page in queryset.exclude(safety_note="").order_by("pk")
            if REPORT_MARKERS.search(page.safety_note)
        ]

    def _incident(self, page: Page, *, dry_run: bool) -> tuple[SafetyIncident | None, bool]:
        cluster_id = cluster_ref(page)
        decision = open_or_link(
            page,
            related=related_pages(page, cluster_id=cluster_id),
            cluster_ref=cluster_id,
            dry_run=dry_run,
        )
        return decision.incident, decision.action == "created"
