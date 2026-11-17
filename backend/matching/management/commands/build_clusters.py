from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from matching.duplicates import DEFAULT_MAX_DISTANCE
from matching.pdq import MIN_QUALITY
from matching.services import build_clusters


class Command(BaseCommand):
    help = "Import human labels and propose video cluster memberships."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--max-distance", type=int, default=DEFAULT_MAX_DISTANCE)
        parser.add_argument("--review-max-distance", type=int, default=None)
        parser.add_argument("--min-score", type=float, default=0.0)
        parser.add_argument("--min-quality", type=int, default=MIN_QUALITY)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            created, proposed = build_clusters(
                options["max_distance"],
                options["min_score"],
                options["min_quality"],
                options["dry_run"],
                review_max_distance=options["review_max_distance"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        prefix = "Dry run: " if options["dry_run"] else ""
        self.stdout.write(f"{prefix}{created} clusters created / {proposed} members proposed")
