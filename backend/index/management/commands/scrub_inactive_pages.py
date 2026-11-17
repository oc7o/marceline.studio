"""Catch up on the scrub that a deactivation is supposed to have done (issue #148).

The two documents that state this deletion as a fact are
`docs/legal/verzeichnis-verarbeitungstaetigkeiten.md` (V06) and `docs/legal/crawl-position.md` § 2,
so the number this command leaves behind is a compliance number, not a tidy-up: `make status`
prints it after every run (`index.retention.scrub_gap`).
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from index.retention import SCRUB_BATCH_SIZE, ScrubBackfillReport, backfill_scrub


class Command(BaseCommand):
    help = (
        "Scrub title, tags and performer credits from pages that were deactivated as untargeted "
        "before the scrub at deactivation existed (ADR 0029 amendment, issue #148). Deletion is "
        "irreversible, so this defaults to a dry run that writes nothing -- pass --apply after "
        "reading it. Never touches an active page, a page inactive for another reason, or a page "
        "that is not safety-clear. Idempotent, batched and resumable: re-run it after an "
        "interruption or a --limit."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run", action="store_true", help="Report counts only (the default)."
        )
        mode.add_argument(
            "--apply", action="store_true", help="Clear the three fields. Irreversible."
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=SCRUB_BATCH_SIZE,
            help=f"Pages per statement (default {SCRUB_BATCH_SIZE}); each batch commits alone.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Stop after this many pages (a first cautious run); the rest is reported as left.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        report = backfill_scrub(
            dry_run=not options["apply"],
            batch_size=options["batch_size"],
            limit=options["limit"],
        )
        self._print(report)

    def _print(self, report: ScrubBackfillReport) -> None:
        mode = " (dry run, nothing written)" if report.dry_run else ""
        self.stdout.write(f"page scrub backfill {report.now.isoformat()}{mode}")
        self.stdout.write(
            f"candidates: {report.candidates} page(s) deactivated as untargeted that still hold "
            "title, tags or performer credits"
        )
        if not report.dry_run:
            self.stdout.write(f"scrubbed: {report.scrubbed} page(s)")
        by_day = ", ".join(f"{day} {n}" for day, n in report.by_day.items()) or "none"
        self.stdout.write(f"  by deactivation day: {by_day}")
        by_reason = ", ".join(f"{reason} {n}" for reason, n in report.by_reason.items()) or "none"
        self.stdout.write(f"  inactive pages holding text, by reason: {by_reason}")
        if report.safety_flagged:
            self.stdout.write(
                f"  safety-flagged: {report.safety_flagged} page(s) hold text and are left "
                "untouched (docs/suppression.md: a flagged page's stored text is never rewritten)"
            )
        self.stdout.write(f"  left after this run: {report.remaining} page(s)")
        if report.remaining and not report.dry_run:
            self.stdout.write("  run again to continue (idempotent, resumable)")
