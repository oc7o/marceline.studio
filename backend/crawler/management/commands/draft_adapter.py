from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

import crawler
from crawler.drafting import draft_lines
from crawler.sites.registry import ADAPTERS

#: Where recorded HTML fixtures live (`CLAUDE.md`, "Adding a site adapter"), one directory per site.
FIXTURE_ROOT = Path(crawler.__file__).resolve().parent / "tests" / "fixtures"


class Command(BaseCommand):
    help = (
        "Draft a site adapter from recorded HTML fixtures (ADR 0043, issue #21): engine "
        "fingerprint, link families, field candidates, search form and removal marker, plus a "
        "KvsSite entry where the site runs KVS. Reads files, writes nothing, requests nothing, "
        "and prints structure only — never a title, a name, a tag or a page's URL."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--domain",
            help=f"A directory under {FIXTURE_ROOT} named after the site (e.g. example.com).",
        )
        parser.add_argument(
            "--fixtures",
            help="Path to a fixture directory instead, named after the site.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        domain, fixtures = options["domain"], options["fixtures"]
        if bool(domain) == bool(fixtures):
            raise CommandError("pass exactly one of --domain and --fixtures")
        directory = Path(fixtures) if fixtures else FIXTURE_ROOT / str(domain)
        if not directory.is_dir():
            raise CommandError(f"no fixture directory at {directory}")
        if not list(directory.glob("*.html")):
            raise CommandError(f"no *.html fixtures in {directory} — record them first")
        for line in draft_lines(directory):
            self.stdout.write(line)
        if directory.name in ADAPTERS:
            self.stdout.write("")
            self.stdout.write(
                f"note: {directory.name} already has a registered adapter — this draft is a "
                "second opinion, not an onboarding"
            )
