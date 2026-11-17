from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from matching.leaks import suggest_leaks
from matching.references import select_references


class Command(BaseCommand):
    help = "Propose ground-truth leak pages for human review."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--reference", type=int)
        parser.add_argument("--creator")
        parser.add_argument("--limit-per-reference", type=int, default=5)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            lines = suggest_leaks(
                select_references(options["creator"], options["reference"]),
                limit_per_reference=options["limit_per_reference"],
                dry_run=options["dry_run"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        for line in lines:
            self.stdout.write(line)
