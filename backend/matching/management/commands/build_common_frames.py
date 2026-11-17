from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from matching.common_frames import COMMON_MAX_DISTANCE, COMMON_MIN_PAGES, build_common_frames
from matching.pdq import MIN_QUALITY


class Command(BaseCommand):
    help = "Group near-identical page frames seen on many unrelated pages (intros, watermarks)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--max-distance", type=int, default=COMMON_MAX_DISTANCE)
        parser.add_argument("--min-pages", type=int, default=COMMON_MIN_PAGES)
        parser.add_argument("--min-quality", type=int, default=MIN_QUALITY)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Also group frames of deactivated pages (labeling queue only; off by default).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            lines = build_common_frames(
                max_distance=options["max_distance"],
                min_pages=options["min_pages"],
                min_quality=options["min_quality"],
                dry_run=options["dry_run"],
                include_inactive=options["include_inactive"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        for line in lines:
            self.stdout.write(line)
