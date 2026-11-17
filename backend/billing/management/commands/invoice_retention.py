"""Delete issued invoices and cancellations past the statutory retention period (ADR 0032).

Eight years, counted from the end of the calendar year of issue (§ 147 Abs. 3 AO, § 257 Abs. 4
HGB, § 14b Abs. 1 UStG — `billing/retention.py`). Idempotent and cron-safe; run `--dry-run` first.
Scheduled daily (`SCHEDULER_INVOICE_RETENTION_HOURS`).
"""

from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from billing.retention import apply_retention


class Command(BaseCommand):
    help = (
        "Delete issued invoices/cancellations past their eight-year retention period "
        "(counted from the end of the year they were issued)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
        parser.add_argument("--date", type=date.fromisoformat, help="pretend today is YYYY-MM-DD")

    def handle(self, *args: Any, **options: Any) -> None:
        today: date = options["date"] or timezone.localdate()
        dry_run: bool = options["dry_run"]
        prefix = "would " if dry_run else ""
        result = apply_retention(today, dry_run=dry_run)
        self.stdout.write(f"{prefix}delete invoices {result.deleted or 'none'}")
        if result.kept_for_cancellation:
            self.stdout.write(
                f"kept (retained cancellation not yet deletable): {result.kept_for_cancellation}"
            )
