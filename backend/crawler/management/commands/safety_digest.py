from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from crawler import safety_digest


class Command(BaseCommand):
    help = (
        "The one daily operator report (docs/safety.md 'Daily operator report'): safety (the "
        "window's blocks by site and by rule, the incident-rule blocks by name, what is waiting "
        "for a human) and crawler (adapters newly reported broken, robots.txt TDM/AI reservations "
        "newly recorded), plus what changed since the window before. Sent once a day even when "
        "every section is quiet. The scheduler runs this once a day (job `safety_digest`); run it "
        "by hand to see what today's mail would say. --dry-run prints the mail and sends nothing."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--hours",
            type=float,
            default=24.0,
            help="Window length ending now, in hours (default 24).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the subject and body that would be mailed; send nothing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        end = timezone.now()
        start = end - timedelta(hours=float(options["hours"]))
        if options["dry_run"]:
            digest = safety_digest.collect(start, end)
            self.stdout.write(f"Subject: {safety_digest.subject(digest)}")
            self.stdout.write("")
            self.stdout.write(safety_digest.body(digest))
            self.stdout.write("")
        self.stdout.write(safety_digest.send_digest(start, end, dry_run=bool(options["dry_run"])))
