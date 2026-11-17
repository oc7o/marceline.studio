from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from index.incidents import (
    AUTO_CLOSE_AFTER_DAYS,
    RETAIN_MONTHS_AFTER_CLOSE,
    RetentionReport,
    apply_retention,
)


class Command(BaseCommand):
    help = (
        "Apply the safety-incident retention policy (docs/safety.md, issue #66): close reported "
        "cases the hotline has not come back on, then delete cases closed longer ago than the "
        "retention period. Idempotent and safe to run from cron. Run --dry-run first. Prints "
        "counts and incident ids only, never a URL or page content."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
        parser.add_argument(
            "--close-after-days",
            type=int,
            default=AUTO_CLOSE_AFTER_DAYS,
            help=(
                "Close a reported case after this long without news "
                f"(default {AUTO_CLOSE_AFTER_DAYS})."
            ),
        )
        parser.add_argument(
            "--retain-months",
            type=int,
            default=RETAIN_MONTHS_AFTER_CLOSE,
            help=(
                "Months a closed case is kept before deletion "
                f"(default {RETAIN_MONTHS_AFTER_CLOSE})."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        report = apply_retention(
            dry_run=options["dry_run"],
            close_after_days=options["close_after_days"],
            retain_months=options["retain_months"],
        )
        self._print(report)

    def _print(self, report: RetentionReport) -> None:
        mode = " (dry run, nothing written)" if report.dry_run else ""
        self.stdout.write(f"safety retention {report.now.isoformat()}{mode}")
        statuses = ", ".join(
            f"{status} {report.by_status.get(status, 0)}"
            for status in ("open", "reported", "closed", "false_positive")
        )
        self.stdout.write(f"incidents: {report.total} total ({statuses})")
        close_verb = "to close" if report.dry_run else "closed"
        self.stdout.write(
            f"{close_verb}: {report.closed} reported case(s) reported before "
            f"{report.close_before.date().isoformat()} ({report.close_after_days} days)"
        )
        delete_verb = "to delete" if report.dry_run else "deleted"
        ids = ", ".join(str(pk) for pk in report.deleted_ids) or "-"
        self.stdout.write(
            f"{delete_verb}: {report.deleted} case(s) closed before "
            f"{report.delete_before.date().isoformat()} ({report.retain_months} months); ids: {ids}"
        )
        self.stdout.write(f"kept: {report.kept} record(s)")
        self.stdout.write(
            f"open cases waiting for a human decision: {report.open_unreported} "
            "(never closed or deleted automatically)"
        )
        if report.deleted and not report.dry_run:
            self.stdout.write(
                "note: a deleted record still exists in encrypted off-host backups for up to "
                "about six months (docs/runbooks/backup-restore.md). A restore does not bring it "
                "back, though: each deletion above is journalled outside the database and "
                "`reapply_deletions` deletes it again (ADR 0057)."
            )
