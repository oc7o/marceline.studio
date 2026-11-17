from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from catalog.models import Site
from catalog.request_report import report_lines


class Command(BaseCommand):
    help = (
        "Real outgoing requests per site: origin budget use and CDN hosts in the rolling 24 h "
        "budget window and over --days, by request type and outcome (issue #74)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only this domain.")
        parser.add_argument("--days", type=int, default=7, help="Reporting period (default 7).")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["days"] < 1:
            raise CommandError("--days must be at least 1")
        domain = options["site"]
        if domain and not Site.objects.filter(domain=domain).exists():
            raise CommandError(f"unknown site {domain}")
        for line in report_lines(timezone.now(), days=options["days"], domain=domain):
            self.stdout.write(line)
