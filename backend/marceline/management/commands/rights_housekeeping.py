"""Expire ended mandates and schedules, then delete closed rights records past retention (ADR 0034).

Idempotent and cron-safe; run `--dry-run` first. Not scheduled yet (operator decision).
"""

from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from marceline.rights.retention import apply_retention
from marceline.rights.services import expire_due


class Command(BaseCommand):
    help = (
        "Move works of ended mandates/schedules to expired, then delete closed rights records "
        "after the end of the third calendar year following their last event (unless held)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
        parser.add_argument("--date", type=date.fromisoformat, help="pretend today is YYYY-MM-DD")

    def handle(self, *args: Any, **options: Any) -> None:
        today: date = options["date"] or timezone.localdate()
        dry_run: bool = options["dry_run"]
        prefix = "would " if dry_run else ""
        expiry = expire_due(today, dry_run=dry_run)
        self.stdout.write(
            f"{prefix}expire {len(expiry.works)} works "
            f"(mandates {[row.pk for row in expiry.mandates]}, "
            f"schedules {[row.pk for row in expiry.schedules]})"
        )
        retention = apply_retention(today, dry_run=dry_run)
        self.stdout.write(
            f"{prefix}delete works {retention.works}, mandates {retention.mandates}, "
            f"proofs {retention.proofs}; kept on legal hold: {retention.held or 'none'}"
        )
