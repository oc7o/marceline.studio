from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from catalog.catalogue_size import record_total
from catalog.models import CatalogueSizeMethod, Site
from index import catalogue_share


class Command(BaseCommand):
    help = (
        "How large each site's catalogue is and what share of it we hold (issue #107, ADR 0049): "
        "per site the site's own stated or derivable total with its provenance, our pages ever "
        "indexed and still active, and both shares. Read-only by default and it never makes a "
        "request; --record enters a total by hand, --refresh stamps today's counts into the "
        "records and alerts the operator about a site that newly crossed the monitoring threshold."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only this domain (enabled or not).")
        parser.add_argument(
            "--record",
            type=int,
            metavar="TOTAL",
            help="Record this catalogue total for --site, by hand, from a recorded note. "
            "Needs --site and --method.",
        )
        parser.add_argument(
            "--method",
            choices=[value for value, _ in CatalogueSizeMethod.choices],
            help="How the recorded total was obtained.",
        )
        parser.add_argument(
            "--source",
            default="",
            help="Where it was read: a path shape or the name of a recorded note. Never a full "
            "URL and never an identifier.",
        )
        parser.add_argument(
            "--note", default="", help="What the figure counts and what it was cross-checked on."
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Stamp today's page counts into the records and alert on a new crossing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        domain = options["site"]
        if domain and not Site.objects.filter(domain=domain).exists():
            raise CommandError(f"no such site: {domain}")
        if options["record"] is not None:
            self._record(options, domain)
        if options["refresh"]:
            _, alerted = catalogue_share.evaluate(now=timezone.now())
            self.stdout.write(f"counts refreshed; {alerted} site(s) newly alerted on.")
        self.stdout.write(catalogue_share.render_text(catalogue_share.measure(domain=domain)))

    def _record(self, options: dict[str, Any], domain: str | None) -> None:
        if not domain:
            raise CommandError("--record needs --site: a total belongs to one site")
        if not options["method"]:
            raise CommandError("--record needs --method: how the figure was obtained")
        if options["record"] <= 0:
            raise CommandError("a catalogue total is a positive number of videos")
        site = Site.objects.get(domain=domain)
        written = record_total(
            site,
            options["record"],
            options["method"],
            source_path=options["source"],
            note=options["note"],
            recorded_by="catalogue_size",
            now=timezone.now(),
        )
        if not written:
            raise CommandError(
                f"{domain} already holds a figure obtained by a method that outranks "
                f"{options['method']}; change it in the admin if it is wrong"
            )
        self.stdout.write(f"recorded {options['record']:,} for {domain} ({options['method']})")
