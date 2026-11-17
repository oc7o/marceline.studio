from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from index.retention import PAGE_RETENTION_MONTHS, PageRetentionReport, apply_retention


class Command(BaseCommand):
    help = (
        "Delete pages that have stayed deactivated (untargeted) longer than --retain-months "
        f"(default {PAGE_RETENTION_MONTHS}); ADR 0029 amendment. Deletion is irreversible, so "
        "this defaults to a dry run that writes nothing -- pass --apply for a real run, after "
        "reviewing the dry run's counts. Idempotent and safe to run from cron."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run", action="store_true", help="Report counts per site only (the default)."
        )
        mode.add_argument(
            "--apply", action="store_true", help="Delete the pages past retention. Irreversible."
        )
        parser.add_argument(
            "--retain-months",
            type=int,
            default=PAGE_RETENTION_MONTHS,
            help=f"Months a deactivated page is kept (default {PAGE_RETENTION_MONTHS}).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        report = apply_retention(
            dry_run=not options["apply"], retain_months=options["retain_months"]
        )
        self._print(report)

    def _print(self, report: PageRetentionReport) -> None:
        mode = " (dry run, nothing written)" if report.dry_run else ""
        self.stdout.write(f"page retention {report.now.isoformat()}{mode}")
        self.stdout.write(f"deactivated pages with a retention clock running: {report.deactivated}")
        by_site = ", ".join(f"{domain} {n}" for domain, n in report.by_site.items()) or "none"
        verb = "to delete" if report.dry_run else "deleted"
        self.stdout.write(
            f"{verb}: {report.due} page(s) deactivated before "
            f"{report.delete_before.date().isoformat()} ({report.retain_months} months); "
            f"by site: {by_site}"
        )
        if report.safety_protected_ids:
            self.stdout.write(
                f"kept: {len(report.safety_protected_ids)} page(s) protected by an open or "
                "reported safety incident"
            )
        if not report.dry_run:
            self.stdout.write(f"deleted: {len(report.deleted_ids)} page(s)")
            if report.protected_ids:
                self.stdout.write(
                    f"kept: {len(report.protected_ids)} page(s) protected by an open notice draft"
                )
