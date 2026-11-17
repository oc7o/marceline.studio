from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from crawler.relink import STORED_CREDITS, relink_flagged_pages


class Command(BaseCommand):
    help = (
        "Issue #3: classify the stored performer credits of a site's pages (noise credits, "
        "compilation posts), store the credit flags a crawl would store today and remove the "
        "creator links they rule out. Never adds links or deletes pages. Dry run unless --apply."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--site", required=True, choices=sorted(STORED_CREDITS), help="Site domain."
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run", action="store_true", help="Report counts only (the default)."
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Write the credit flags, remove the ruled-out links, recount breadcrumbs.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            report = relink_flagged_pages(options["site"], dry_run=not options["apply"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write("Dry run, nothing written." if report.dry_run else "Applied.")
        for line in report.lines():
            self.stdout.write(line)
