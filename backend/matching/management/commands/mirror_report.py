from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from matching.mirrors import mirror_report
from matching.pdq import MIN_QUALITY
from matching.references import LOOSE_MAX_DISTANCE, STRICT_MAX_DISTANCE


class Command(BaseCommand):
    help = (
        "Are there mirrored frames in the index (issue #8)? Read-only: no request, no write. "
        "Reports which reference orientation wins per page frame, and which page pairs only a "
        "derived mirror would find."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--max-distance", type=int, default=STRICT_MAX_DISTANCE)
        parser.add_argument("--reference-distance", type=int, default=LOOSE_MAX_DISTANCE)
        parser.add_argument("--min-quality", type=int, default=MIN_QUALITY)
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args: Any, **options: Any) -> None:
        for line in mirror_report(
            max_distance=options["max_distance"],
            reference_distance=options["reference_distance"],
            min_quality=options["min_quality"],
            limit=options["limit"],
        ):
            self.stdout.write(line)
