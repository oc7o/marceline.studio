from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from matching.m2 import m2_report
from matching.references import STRICT_MAX_DISTANCE, select_references


class Command(BaseCommand):
    help = "Measure M2 recall and false matches against human ground truth."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--creator")
        parser.add_argument("--max-distance", type=int, default=STRICT_MAX_DISTANCE)

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            lines = m2_report(
                select_references(options["creator"]), max_distance=options["max_distance"]
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        for line in lines:
            self.stdout.write(line)
