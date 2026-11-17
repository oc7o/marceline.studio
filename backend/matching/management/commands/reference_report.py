from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from matching.references import reference_report, select_references


class Command(BaseCommand):
    help = "Print reference review counts and cluster coverage."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--creator")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            lines = reference_report(select_references(options["creator"]))
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        for line in lines:
            self.stdout.write(line)
