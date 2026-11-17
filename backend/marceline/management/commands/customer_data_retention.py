"""Delete what an ended pilot, or a removed original, left behind (ADR 0045, issue #85).

    manage.py customer_data_retention               # dry run: counts only, nothing written
    manage.py customer_data_retention --apply       # irreversible

Two scopes, both `CUSTOMER_DATA_RETENTION_DAYS` after a date a human set: an organisation whose
pilot ended (`end_pilot`) and one original marked for removal (`remove_original`, or an owner
switch through `assign_originals`). Pages, the index, invoices, legal holds and safety incidents
are never touched — what is kept back is reported by its code, not silently skipped.

Idempotent and safe to run from cron; the scheduler runs it daily, last, once
`CUSTOMER_DATA_DELETION_ENABLED` is on.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from marceline.deletion.retention import RetentionReport, ScopeReport, apply_retention


class Command(BaseCommand):
    help = (
        "Delete the data of organisations whose pilot ended, and of originals marked for "
        "removal, once the retention window is up (ADR 0045). Deletion is irreversible, so this "
        "defaults to a dry run that writes nothing -- pass --apply after reading it."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run", action="store_true", help="Report counts only (the default)."
        )
        mode.add_argument("--apply", action="store_true", help="Delete. Irreversible.")

    def handle(self, *args: Any, **options: Any) -> None:
        report = apply_retention(dry_run=not options["apply"])
        self._print(report)

    def _print(self, report: RetentionReport) -> None:
        mode = " (dry run, nothing written)" if report.dry_run else ""
        self.stdout.write(f"customer data retention {report.now.isoformat()}{mode}")
        self.stdout.write(
            f"window: {report.window_days} day(s); pending marks: "
            f"{report.pending_organisations} organisation(s), "
            f"{report.pending_originals} original(s)"
        )
        if not report.due:
            self.stdout.write("nothing is due")
            return
        for scope in report.organisations:
            self._print_scope(scope, report.dry_run)
        for scope in report.originals:
            self._print_scope(scope, report.dry_run)

    def _print_scope(self, scope: ScopeReport, dry_run: bool) -> None:
        subject = (
            f"organisation {scope.organisation_pk}"
            if scope.scope == "organisation"
            else f"organisation {scope.organisation_pk}, file {scope.sha256[:12]}…"
        )
        verb = "would delete" if dry_run else "deleted"
        counts = ", ".join(f"{label} {n}" for label, n in sorted(scope.counts.items())) or "nothing"
        self.stdout.write(
            f"{subject} (marked {scope.marked_at.date().isoformat()}, due "
            f"{scope.due_at.date().isoformat()}): {verb} {counts}"
        )
        if scope.kept:
            self.stdout.write(f"  kept: {', '.join(sorted(set(scope.kept)))}")
        if not scope.complete:
            self.stdout.write("  mark stays pending; the job tries again next run")
