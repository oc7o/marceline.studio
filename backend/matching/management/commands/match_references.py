from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from matching import embedding_candidates
from matching.pdq import MIN_QUALITY
from matching.references import (
    LOOSE_MAX_DISTANCE,
    STRICT_MAX_DISTANCE,
    match_references,
    select_references,
)


class Command(BaseCommand):
    help = (
        "Match reference fingerprints against page frames across all sites. Scans only new page "
        "frames and new references unless --full (or the settings changed). -v 2 lists every "
        "creator-linked page left unmatched."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--reference", type=int)
        parser.add_argument("--creator")
        parser.add_argument("--max-distance", type=int, default=STRICT_MAX_DISTANCE)
        parser.add_argument("--min-quality", type=int, default=MIN_QUALITY)
        parser.add_argument("--loose-max-distance", type=int, default=LOOSE_MAX_DISTANCE)
        parser.add_argument(
            "--full",
            action="store_true",
            help="Rescan every page frame against every reference (rebuild the scan cache).",
        )
        parser.add_argument(
            "--embedding-candidates",
            action="store_true",
            help="Also refresh the second signal's candidates (ADR 0037), whatever "
            "MATCHING_EMBEDDINGS_ENABLED says. They never become evidence.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            lines = match_references(
                select_references(options["creator"], options["reference"]),
                max_distance=options["max_distance"],
                min_quality=options["min_quality"],
                loose_max_distance=options["loose_max_distance"],
                full=options["full"],
                list_unmatched=options["verbosity"] >= 2,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        for line in lines:
            self.stdout.write(line)
        # After the real matching, never instead of it: the second signal only proposes rows a
        # human still has to judge, and a PDQ match on the same pair always wins (ADR 0037).
        if options["embedding_candidates"] or settings.MATCHING_EMBEDDINGS_ENABLED:
            report = embedding_candidates.refresh(top_k=settings.MATCHING_EMBEDDING_TOP_K)
            self.stdout.write(report.line())
            if reason := report.empty_reason():
                self.stdout.write(f"  nothing to compare: {reason}")
        # The third signal is deliberately absent here (ADR 0040 amendment 2026-09-18): it
        # produces no evidence and writes nothing, so it has no business in the matching path.
        # It aims browser captures instead - `triage_browser_capture`, run daily by the scheduler.
