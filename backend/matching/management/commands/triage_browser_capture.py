from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from matching.capture_triage import NO_GATE_SIGNALS, budget_view, refresh_candidates, triage_report


class Command(BaseCommand):
    help = (
        "Refresh the browser-capture triage queue (ADR 0016). Nothing is captured: this only "
        "scores, queues, updates and expires candidates."
    )
    # The scheduler passes crawler gate signals (matching may not import crawler). Rows refreshed
    # without them are marked gate_checked=false and never returned by next_capture_batch.
    stealth_options = ("gate_signals",)

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
        parser.add_argument("--limit", type=int, help="Keep at most N queued candidates.")
        parser.add_argument(
            "--metadata-candidates",
            action="store_true",
            help=(
                "Count the third signal's candidates as a capture source for this run, whatever "
                "MATCHING_METADATA_CANDIDATES_ENABLED says (ADR 0040 amendment). They aim a "
                "fetch; they never become evidence and never reach a human."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        now = timezone.now()
        try:
            result = refresh_candidates(
                now,
                gate_signals=options.get("gate_signals") or NO_GATE_SIGNALS,
                limit=options["limit"],
                dry_run=options["dry_run"],
                metadata=True if options["metadata_candidates"] else None,
            )
            budget = budget_view(now)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        for line in triage_report(result, budget):
            self.stdout.write(line)
