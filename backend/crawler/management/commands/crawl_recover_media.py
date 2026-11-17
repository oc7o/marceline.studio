from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from crawler.services import recover_expired_media


class Command(BaseCommand):
    help = (
        "Re-enqueue the listing or detail tasks that produced expired media links, so the crawler "
        "fetches fresh URLs. The crawl worker does this by itself for the last two days."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only media of pages on this domain.")
        parser.add_argument(
            "--max-age-hours",
            type=int,
            help="Only media that expired within N hours (default: any age).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        hours = options["max_age_hours"]
        if hours is not None and hours < 0:
            raise CommandError("--max-age-hours must be non-negative.")
        count = recover_expired_media(
            site_domain=options["site"],
            max_age=timedelta(hours=hours) if hours is not None else None,
        )
        self.stdout.write(f"Enqueued {count} task(s).")
