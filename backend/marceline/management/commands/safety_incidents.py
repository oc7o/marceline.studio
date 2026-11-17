from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from marceline.safety_incidents import SyncReport, sync_incidents


class Command(BaseCommand):
    help = (
        "Open a safety incident for every blocked page that does not have one yet, deduplicated "
        "across re-crawls, mirrors and term-list changes (docs/safety.md, issue #66). Idempotent; "
        "run --dry-run first. Prints counts and ids only, never page content."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only blocked pages of this domain.")
        parser.add_argument("--limit", type=int, help="At most this many pages.")
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
        parser.add_argument(
            "--list", action="store_true", help="Also list page id, decision and incident id."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        report = sync_incidents(
            site_domain=options["site"], limit=options["limit"], dry_run=options["dry_run"]
        )
        self._print(report, list_pages=options["list"])

    def _print(self, report: SyncReport, *, list_pages: bool) -> None:
        mode = " (dry run, nothing written)" if report.dry_run else ""
        self.stdout.write(f"scanned {report.scanned} blocked page(s){mode}")
        self.stdout.write(
            "skipped, blocked by a rule that opens no incident "
            f"(auto_block, customer_release_queue): {report.not_reportable}"
        )
        opened = "to open" if report.dry_run else "opened"
        self.stdout.write(f"incidents {opened}: {report.created}")
        linked = ", ".join(f"{kind}={n}" for kind, n in sorted(report.linked.items())) or "-"
        self.stdout.write(
            f"duplicates folded into an existing case: {report.linked_total} ({linked})"
        )
        self.stdout.write(f"settled by an earlier human decision, left alone: {report.settled}")
        if list_pages:
            for page_id, decision in report.decisions:
                incident = decision.incident_id if decision.incident_id is not None else "-"
                self.stdout.write(
                    f"page={page_id} {decision.action:<8} {decision.evidence:<12} "
                    f"incident={incident}"
                )
