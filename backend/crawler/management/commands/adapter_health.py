from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from catalog.models import Site
from crawler import adapter_alerts, adapter_health


class Command(BaseCommand):
    help = (
        "Report, per site, whether its adapter is still extracting (issue #22): detail parses "
        "that yielded no page, no title or no media, against the site's own recent baseline. "
        "Writes one `crawler.AdapterHealth` row per site and mails the operator about a new "
        "break; --dry-run writes and sends nothing."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only this domain (enabled or not).")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the report only: no verdict stored, no operator alert.",
        )
        parser.add_argument(
            "--no-alert",
            action="store_true",
            help="Store the verdicts but do not mail or log an alert about a new break.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        domain = options["site"]
        if domain and not Site.objects.filter(domain=domain).exists():
            raise CommandError(f"no such site: {domain}")
        write = not options["dry_run"]
        reports = adapter_health.evaluate_all(domain=domain, write=write)
        self.stdout.write(adapter_health.render_text(reports))
        if write and not options["no_alert"]:
            notified = adapter_alerts.notify_broken(reports)
            if notified:
                self.stdout.write(f"Alerted the operator about {notified} broken adapter(s).")
