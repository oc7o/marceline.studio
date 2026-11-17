"""Development: retry pending production cache purges, and sweep recent safety flags (ADR 0028).

`--flagged-within-minutes N` first queues one `delete` purge for every page linked to an original
whose safety state moved to non-clear within the last N minutes — the catch-all for flags written
by bulk updates (`safety_scan`) that no model signal sees. Idempotent on production. Cron-safe:
run it every few minutes with N a little larger than the interval.
"""

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from marceline import cache_purge


class Command(BaseCommand):
    help = "Deliver pending cache purges to production; optionally sweep recent safety flags."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--flagged-within-minutes", type=int, default=None)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args: Any, **options: Any) -> None:
        if not cache_purge.sending():
            raise CommandError("purging is off: needs MARCELINE_ROLE=dev and CACHE_PURGE_BASE_URL")
        minutes = options["flagged_within_minutes"]
        if minutes is not None:
            rows = cache_purge.sweep_flagged_pages(
                within=timedelta(minutes=minutes), now=timezone.now()
            )
            count = sum(len(row.page_ids) for row in rows)
            split = f" in {len(rows)} purges" if len(rows) > 1 else ""
            self.stdout.write(f"safety sweep: {count} page(s) queued for purge{split}")
        delivered, pending = cache_purge.deliver_pending(limit=options["limit"])
        self.stdout.write(f"purges delivered: {delivered}, still pending: {pending}")
        if pending:
            raise CommandError(f"{pending} purge(s) still pending")
