from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from catalog import safety_rules
from crawler.services import SafetyScanReport, scan_pages
from index.models import Page
from marceline.safety_incidents import SyncReport, sync_incidents
from marceline.safety_rules import ReleaseQueueReport, sync_release_queue

RULE_ACTIONS = (
    safety_rules.AUTO_BLOCK,
    safety_rules.AUTO_BLOCK_AND_INCIDENT,
    safety_rules.CUSTOMER_RELEASE_QUEUE,
)


class Command(BaseCommand):
    help = (
        "Check stored pages against the safety terms, apply statuses, and cancel media and delete "
        "frame hashes of blocked pages (docs/safety.md). A page blocked by an incident rule keeps "
        "its hashes on the case instead, so the same material is not reported twice. Applies the "
        "rule actions: opens incidents for auto_block_and_incident pages and (re)builds the "
        "customer release queue. Prints counts, never page content."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only pages of this domain.")
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
        parser.add_argument(
            "--list", action="store_true", help="Also list flagged page ids, sites and categories."
        )
        parser.add_argument(
            "--gate-report",
            action="store_true",
            help="Per site, how many pages may be fingerprinted and how many stay text-only.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = options["dry_run"]
        report = scan_pages(site_domain=options["site"], dry_run=dry_run)
        if dry_run:
            # Unsaved copies carrying the verdict the scan would store.
            incidents = sync_incidents(pages=report.incident_pages, dry_run=True)
            queue = sync_release_queue(pages=report.release_pages, dry_run=True)
        else:
            stored = list(
                Page.objects.select_related("site")
                .filter(pk__in=[page.pk for page in report.incident_pages])
                .order_by("pk")
            )
            incidents = sync_incidents(
                pages=[page for page in stored if safety_rules.opens_incident(page.safety_matches)],
                # The hashes the scan took off those pages before deleting them (ADR 0011
                # amendment 2026-09-17): the case keeps them, so the same material found again is
                # one case and one report.
                hashes=report.retained_hashes,
            )
            queue = sync_release_queue()
        self._print(report, list_pages=options["list"], gate_report=options["gate_report"])
        self._print_rules(report, incidents, queue)

    def _print_rules(
        self, report: SafetyScanReport, incidents: SyncReport, queue: ReleaseQueueReport
    ) -> None:
        """Counts per rule action (catalog/safety_rules.py). Never titles, URLs or names."""
        dry = report.dry_run
        self.stdout.write(f"safety rules (rule set {report.rule_version}):")
        for action in RULE_ACTIONS:
            blocked = sum(n for (a, _), n in report.rule_blocked.items() if a == action)
            newly = sum(n for (a, _), n in report.rule_newly_blocked.items() if a == action)
            categories = ", ".join(
                f"{c}={n}" for (a, c), n in sorted(report.rule_blocked.items()) if a == action
            )
            verb = "to block" if dry else "blocked"
            self.stdout.write(
                f"  {action}: {blocked} blocked page(s), {newly} {verb} by this scan"
                + (f" ({categories})" if categories else "")
            )
            if action == safety_rules.AUTO_BLOCK_AND_INCIDENT:
                opened = "to open" if dry else "opened"
                self.stdout.write(
                    f"    incidents {opened}: {incidents.created}, folded into an existing case: "
                    f"{incidents.linked_total}, settled earlier by a human: {incidents.settled}"
                )
            if action == safety_rules.CUSTOMER_RELEASE_QUEUE:
                self.stdout.write(
                    f"    queued for staff (customer credited): {queue.queued} "
                    f"(new: {queue.newly_queued}); kept blocked, no customer credit: "
                    f"{queue.uncredited}; kept blocked, another safety hit: {queue.other_hits}; "
                    f"kept blocked by staff: {queue.staff_kept_blocked}; "
                    f"leaving the queue: {queue.dequeued}"
                )
        revoked = "to revoke" if dry else "revoked"
        self.stdout.write(f"  customer releases {revoked}: {report.releases_revoked}")

    def _print(
        self, report: SafetyScanReport, *, list_pages: bool, gate_report: bool = False
    ) -> None:
        mode = " (dry run, nothing written)" if report.dry_run else ""
        self.stdout.write(f"scanned {report.scanned} page(s){mode}")
        for status in ("blocked", "review"):
            self.stdout.write(f"{status}: {report.by_status[status]} page(s)")
            categories = sorted(
                (cat, n) for (s, cat), n in report.by_category.items() if s == status
            )
            sites = sorted((site, n) for (s, site), n in report.by_site.items() if s == status)
            if categories:
                self.stdout.write("  categories: " + ", ".join(f"{c}={n}" for c, n in categories))
            if sites:
                self.stdout.write("  sites: " + ", ".join(f"{s}={n}" for s, n in sites))
        if report.changed:
            changes = ", ".join(f"{k}={n}" for k, n in sorted(report.changed.items()))
            self.stdout.write(f"status changes: {changes}")
        verb = "to cancel" if report.dry_run else "cancelled"
        hashes = "to delete" if report.dry_run else "deleted"
        kept = "to keep on a case" if report.dry_run else "kept on a case"
        self.stdout.write(
            f"blocked-page media {verb}: {report.media_cancelled}, "
            f"frame hashes {hashes}: {report.hashes_deleted} "
            f"(of them {kept}: {report.hashes_retained})"
        )
        gated = "to gate" if report.dry_run else "gated"
        eligible = "to make eligible" if report.dry_run else "made eligible"
        self.stdout.write(
            f"media {gated} by the safety gate: {report.media_gated}, "
            f"released: {report.media_released}, {eligible}: {report.media_eligible}"
        )
        if gate_report:
            self._print_gate_report(report)
        if list_pages:
            for page_id, domain, page_status, page_categories in report.flagged:
                labels = ",".join(page_categories)
                self.stdout.write(f"{page_status:<8} page={page_id} {domain} {labels}")

    def _print_gate_report(self, report: SafetyScanReport) -> None:
        """What the media gate costs, per site (ADR 0018)."""
        domains = sorted(set(report.hashed_by_site) | set(report.text_only_by_site))
        self.stdout.write("media gate, pages per site:")
        self.stdout.write(f"  {'site':<24}{'hashed':>8}{'text-only':>11}{'text-only %':>13}")
        for domain in domains:
            hashed = report.hashed_by_site[domain]
            text_only = report.text_only_by_site[domain]
            total = hashed + text_only
            share = f"{100 * text_only / total:.1f}%" if total else "n/a"
            self.stdout.write(f"  {domain:<24}{hashed:>8}{text_only:>11}{share:>13}")
        hashed_total = sum(report.hashed_by_site.values())
        text_only_total = sum(report.text_only_by_site.values())
        total = hashed_total + text_only_total
        share = f"{100 * text_only_total / total:.1f}%" if total else "n/a"
        self.stdout.write(f"  {'TOTAL':<24}{hashed_total:>8}{text_only_total:>11}{share:>13}")
        if report.text_only_reasons:
            reasons = ", ".join(
                f"{reason}={count}" for reason, count in sorted(report.text_only_reasons.items())
            )
            self.stdout.write(f"  text-only reasons: {reasons}")
