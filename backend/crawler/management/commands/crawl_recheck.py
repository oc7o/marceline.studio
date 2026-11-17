from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from crawler.services import RECHECK_PRIORITY, recheck_pages


class Command(BaseCommand):
    help = "Enqueue existing pages for an availability re-check."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only this domain.")
        parser.add_argument("--older-than-hours", type=int, default=24)
        parser.add_argument("--limit", type=int)
        parser.add_argument("--include-removed", action="store_true")
        parser.add_argument(
            "--never-checked",
            action="store_true",
            help="Only pages never availability-checked (ignores --older-than-hours).",
        )
        parser.add_argument(
            "--priority",
            type=int,
            help=f"Priority of the queued tasks (reviewer rechecks use {RECHECK_PRIORITY}).",
        )
        parser.add_argument(
            "--only-removed",
            action="store_true",
            help="Only pages recorded as removed, to notice a re-upload at the same URL.",
        )
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Also pages deactivated as untargeted (issue #71); skipped by default.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["older_than_hours"] < 0 or (
            options["limit"] is not None and options["limit"] < 0
        ):
            raise CommandError("Hours and limit must be non-negative.")
        count = recheck_pages(
            site_domain=options["site"],
            older_than_hours=options["older_than_hours"],
            limit=options["limit"],
            include_removed=options["include_removed"],
            never_checked=options["never_checked"],
            priority=options["priority"],
            include_inactive=options["include_inactive"],
            only_removed=options["only_removed"],
        )
        self.stdout.write(f"Enqueued {count} task(s).")
