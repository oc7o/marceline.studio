from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from marceline.hosting import lookup_hosting


class Command(BaseCommand):
    help = (
        "Fill or refresh hosting records (who hosts, abuse contact and its source, registrar, CDN, "
        "country) for hosts of customer findings first, then every enabled site (ADR 0027). DNS "
        "and RDAP registries only; the hosts themselves are never requested. Records younger than "
        "HOSTING_REFRESH_DAYS are skipped unless --refresh. Prints hostnames only."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--domain", help="Only this hostname (a site or a customer CDN host).")
        parser.add_argument("--limit", type=int, help="At most this many lookups.")
        parser.add_argument(
            "--refresh", action="store_true", help="Look up again even if the record is fresh."
        )
        parser.add_argument("--dry-run", action="store_true", help="List what is due; no requests.")

    def handle(self, *args: Any, **options: Any) -> None:
        limit = options["limit"]
        if limit is not None and limit < 1:
            raise CommandError("--limit must be at least 1")
        report = lookup_hosting(
            domain=options["domain"],
            limit=limit,
            refresh=options["refresh"],
            dry_run=options["dry_run"],
        )
        if report.unknown_domain is not None:
            raise CommandError(
                f"{report.unknown_domain!r} is neither an enabled site nor a host of a customer "
                "finding"
            )
        mode = " (dry run, no requests, nothing written)" if report.dry_run else ""
        self.stdout.write(f"{report.candidates} candidate host(s), {len(report.due)} due{mode}")
        if report.dry_run:
            for hostname in report.due:
                self.stdout.write(f"  due: {hostname}")
            return
        for hostname, status in report.results:
            self.stdout.write(f"  {status}: {hostname}")
        counts: dict[str, int] = {}
        for _, status in report.results:
            counts[status] = counts.get(status, 0) + 1
        summary = ", ".join(f"{status}={n}" for status, n in sorted(counts.items())) or "none"
        self.stdout.write(f"looked up {len(report.results)}: {summary}")
