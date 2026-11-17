from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from marceline.provider_directory import report_lines


class Command(BaseCommand):
    help = (
        "Provider directory report (ADR 0027 amendment 2026-09-17), staff only, writes nothing, "
        "makes no request: channels due for re-checking, then per host of a customer finding "
        "(or --hostname) each provider role with nexus, function and suggested notice channel, "
        "and the roles the directory does not cover yet."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--hostname",
            action="append",
            help="Report this host instead of the customer finding hosts (repeatable).",
        )
        parser.add_argument(
            "--due-only", action="store_true", help="Only the channels due for re-checking."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        for line in report_lines(
            timezone.now(), hostnames=options["hostname"], due_only=options["due_only"]
        ):
            self.stdout.write(line)
