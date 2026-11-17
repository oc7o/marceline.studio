from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from crawler.discovery.domains import normalize_domain
from crawler.discovery.probe import probe
from crawler.models import SourceCandidate


class Command(BaseCommand):
    help = "Fetch robots and, if allowed, homepage only for proposed unknown/tube candidates."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--domain")
        parser.add_argument(
            "--retry-failed",
            action="store_true",
            help="Only re-probe previous redirect-only failures.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["limit"] < 0:
            raise CommandError("--limit must be nonnegative")
        # Naming a domain is an explicit operator decision, so it reaches any candidate: an
        # approved domain still needs a probe before anyone writes an adapter for it.
        candidates = SourceCandidate.objects.order_by("pk")
        if not options["domain"]:
            candidates = candidates.filter(status="proposed", kind__in=("unknown", "tube"))
        if options["retry_failed"]:
            candidates = candidates.filter(probed_at__isnull=False).filter(
                Q(blocked_reason__startswith="redirect")
                | Q(
                    http_status__gte=300,
                    http_status__lt=400,
                    blocked_reason__in=("", "robots unavailable"),
                )
            )
        if options["domain"]:
            candidates = candidates.filter(domain=normalize_domain(options["domain"]))
        count = 0
        for candidate in candidates[: options["limit"]]:
            probe(candidate, force=bool(options["domain"]))
            count += 1
        self.stdout.write(f"Probed {count} candidates.")
