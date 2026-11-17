from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from catalog.media_shapes import shape_lines
from catalog.models import MediaUrlShape, Site


class Command(BaseCommand):
    help = (
        "Media URL shapes that keep answering 404 per site, and what the breaker does about them "
        "(ADR 0041). `--clear` resets one shape; it then re-learns whatever is true."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only this domain.")
        parser.add_argument(
            "--clear",
            metavar="SHAPE",
            help="Forget this shape's counters and suppression (needs --site). Exactly the shape "
            "as the report prints it.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        domain = options["site"]
        sites = Site.objects.order_by("domain")
        if domain:
            sites = sites.filter(domain=domain)
            if not sites.exists():
                raise CommandError(f"unknown site {domain}")
        if options["clear"]:
            if not domain:
                raise CommandError("--clear needs --site: a shape is only ever suppressed per site")
            deleted, _ = MediaUrlShape.objects.filter(
                site__domain=domain, shape=options["clear"]
            ).delete()
            if not deleted:
                raise CommandError(f"no such shape on {domain}")
            self.stdout.write(f"cleared {options['clear']} on {domain}")
            return
        now = timezone.now()
        printed = False
        for site in sites:
            lines = shape_lines(site, now)
            if lines:
                printed = True
                self.stdout.write(site.domain)
                for line in lines:
                    self.stdout.write(line)
        if not printed:
            self.stdout.write("no media URL shape has answered 404 yet")
