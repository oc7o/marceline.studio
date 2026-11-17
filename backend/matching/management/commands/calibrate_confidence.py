from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from matching.calibration import calibrate_confidence


class Command(BaseCommand):
    help = "Calibrate ReferenceMatch confidence bands from human decisions."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--replace-override",
            action="store_true",
            help="Store the derived thresholds even though the ones in force were set by hand. "
            "Without it an operator override is kept and the run stores nothing (issue #90).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        for line in calibrate_confidence(
            dry_run=options["dry_run"], replace_override=options["replace_override"]
        ):
            self.stdout.write(line)
