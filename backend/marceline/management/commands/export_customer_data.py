"""Write the export a customer is offered before their data is deleted (ADR 0045, issue #85).

    manage.py export_customer_data --organisation "Test Customer" --out /tmp/export.json

Staff-triggered: the operator runs it and sends the file. It reads only — except for stamping
`exported_at` on the organisation's ending, when one is marked, so `end_pilot --list` shows whether
the promise was kept. Never writes into the repository: `--out` has no default that would.
"""

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from marceline.app.ownership import organisation_named
from marceline.deletion.export import export_document, stamp_exported_at


class Command(BaseCommand):
    help = (
        "Write one organisation's export (ADR 0045): their API documents as they were shown "
        "them, their originals' metadata and fingerprints, and their notice drafts. "
        "Hashes and ids only."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organisation", required=True, metavar="NAME")
        parser.add_argument(
            "--out", required=True, metavar="PATH", help="File to write. It is overwritten."
        )
        parser.add_argument(
            "--indent", type=int, default=2, help="JSON indentation; 0 for one long line."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            organisation = organisation_named(options["organisation"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        now = timezone.now()
        document = export_document(organisation, now=now)
        path = Path(options["out"]).expanduser()
        try:
            path.write_text(
                json.dumps(document, indent=options["indent"] or None, ensure_ascii=False)
            )
        except OSError as exc:
            raise CommandError(f"could not write {path}: {exc.strerror}") from exc
        updated = stamp_exported_at(organisation, now)
        originals = len(document["fingerprints"]["originals"])
        frames = sum(len(entry["frames"]) for entry in document["fingerprints"]["originals"])
        self.stdout.write(
            f"organisation {organisation.pk}: {len(document['documents'])} customer document(s), "
            f"{originals} original(s) with {frames} frame hash(es), "
            f"{len(document['notice_drafts'])} notice draft(s)"
        )
        self.stdout.write(f"written to {path} ({path.stat().st_size} bytes)")
        if updated:
            self.stdout.write("stamped exported_at on this organisation's ending")
