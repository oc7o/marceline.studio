import hashlib
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from catalog.policy_audit import policy_actor
from catalog.services import seed_sources
from catalog.sources import parse_sources


class Command(BaseCommand):
    help = "Load sites and creators from sources.md into the catalog (idempotent)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--path",
            default=str(Path(settings.BASE_DIR).parent / "sources.md"),
            help="Path to sources.md (default: repository root).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        path = Path(options["path"])
        if not path.is_file():
            raise CommandError(f"{path} not found")
        text = path.read_text()
        sources = parse_sources(text)
        # This run re-applies the reviewed crawl policy of `catalog/sources.py` wholesale, so it can
        # change `enabled`, the delay and the budgets of any site. Name the actor and the revision
        # of the file it came from, or the audit trail would read `database` (issue #106). The
        # revision is the digest of `sources.md` itself: available offline, in a worktree and in a
        # container, where a git revision is not.
        revision = hashlib.sha256(text.encode()).hexdigest()[:12]
        with policy_actor("seed_sources", reason=f"sources.md sha256:{revision}"):
            report = seed_sources(sources)
        self.stdout.write(
            f"{len(sources.sites)} site(s) ({report.sites_created} new), "
            f"{len(sources.creators)} creator(s) ({report.creators_created} new)"
        )
