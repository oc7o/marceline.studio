"""Re-apply the deletion journal to a restored database (ADR 0057, DPIA R24, issue #137)."""

from datetime import datetime
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from marceline.deletion_replay import JournalUnavailable, ReplayReport, replay


class Command(BaseCommand):
    help = (
        "Replay the deletion journal against this database: re-suppress, re-scrub, re-delete and "
        "re-insert what a restore from backup rolled back (ADR 0057). Dry run by default — run it "
        "and read it, then run it with --apply. **Run this before the restored database serves "
        "anything** (docs/runbooks/backup-restore.md). Reports per kind: journal entries, already "
        "in effect, re-applied, could not be applied, needs a human. Prints ids, digests, counts "
        "and reason codes only — never a name, a title or a URL."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually re-apply. Without it nothing is written (the default).",
        )
        parser.add_argument(
            "--since",
            default="",
            help="Only entries from this moment on (ISO date or datetime). Use the dump's "
            "timestamp when only the deletions after it can have been rolled back.",
        )
        parser.add_argument(
            "--journal",
            default="",
            help="Read the journal from this directory instead of DELETION_JOURNAL_DIR (for a "
            "copy restored out of the off-host repository).",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Print the one summary line only (what the weekly restore check reads).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        since = _moment(options["since"]) if options["since"] else None
        directory = Path(options["journal"]) if options["journal"] else None
        try:
            report = replay(
                dry_run=not options["apply"], since=since, directory=directory, now=timezone.now()
            )
        except JournalUnavailable as error:
            raise CommandError(str(error)) from error
        if not options["quiet"]:
            self._print(report)
        self.stdout.write(report.summary)
        if report.failed or report.unreadable:
            # A journal entry nobody could apply means the restored database is still carrying
            # something that was deleted. That is the risk this command exists for, so it fails.
            raise CommandError(
                f"{report.failed} journal entr(ies) could not be applied and "
                f"{len(report.unreadable)} line(s) could not be read"
            )

    def _print(self, report: ReplayReport) -> None:
        mode = "dry run, nothing written" if report.dry_run else "APPLYING"
        self.stdout.write(f"reapply_deletions {report.now.isoformat()} ({mode})")
        self.stdout.write(f"database: {report.database}")
        self.stdout.write(f"journal:  {report.journal_dir}")
        if report.since is not None:
            self.stdout.write(f"since:    {report.since.isoformat()}")
        self.stdout.write("")
        header = ("kind", "entries", "in effect", "re-apply", "failed", "human")
        self.stdout.write("{:<30} {:>8} {:>10} {:>9} {:>7} {:>6}".format(*header))
        for outcome in report.outcomes:
            if not outcome.entries:
                continue
            self.stdout.write(
                f"{outcome.kind:<30} {outcome.entries:>8} {outcome.already:>10} "
                f"{outcome.reapplied:>9} {len(outcome.failed):>7} {len(outcome.needs_human):>6}"
            )
        for outcome in report.outcomes:
            for key, reason in outcome.failed:
                self.stdout.write(
                    self.style.ERROR(f"COULD NOT APPLY {outcome.kind} {key}: {reason}")
                )
            for key, reason in outcome.needs_human:
                self.stdout.write(
                    self.style.WARNING(f"NEEDS A HUMAN   {outcome.kind} {key}: {reason}")
                )
        for problem in report.unreadable:
            self.stdout.write(self.style.ERROR(f"UNREADABLE LINE {problem}"))
        if report.dry_run and (report.reapplied or report.needs_human):
            self.stdout.write("")
            self.stdout.write("nothing was written: re-run with --apply")


def _moment(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise CommandError(f"--since {text!r} is not an ISO date or datetime") from error
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
