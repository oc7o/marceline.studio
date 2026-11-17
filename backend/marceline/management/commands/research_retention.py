"""Retention for the research side: discovery query text, prospect research, catalogue names."""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from crawler.retention import QueryRetentionReport, apply_query_retention
from marceline.name_retention import (
    NameRetentionReport,
    ProspectRetentionReport,
    RetentionRefused,
    apply_name_retention,
    apply_prospect_retention,
)

PASSES = ("queries", "prospects", "names", "all")


class Command(BaseCommand):
    help = (
        "ADR 0048 (issues #103, #104): scrub discovery query text past its period, forget prospect "
        "research nobody acted on, and delete catalogue names nothing uses any more. Counts, ids "
        "and reason codes only -- never a name, a query or a domain. Deletion is irreversible, so "
        "this defaults to a dry run that writes nothing; pass --apply after reading it."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run", action="store_true", help="Report counts only (the default)."
        )
        mode.add_argument("--apply", action="store_true", help="Really scrub and delete.")
        parser.add_argument(
            "--pass",
            dest="which",
            choices=PASSES,
            default="all",
            help="Which pass to run (default: all, in the order queries, prospects, names).",
        )
        parser.add_argument(
            "--query-days",
            type=int,
            default=settings.SEARCH_QUERY_RETENTION_DAYS,
            help="Days a discovery query's text is kept "
            f"(default {settings.SEARCH_QUERY_RETENTION_DAYS}).",
        )
        parser.add_argument(
            "--prospect-days",
            type=int,
            default=settings.PROSPECT_RESEARCH_RETENTION_DAYS,
            help="Days a prospect's research is kept "
            f"(default {settings.PROSPECT_RESEARCH_RETENTION_DAYS}).",
        )
        parser.add_argument(
            "--name-days",
            type=int,
            default=settings.CREATOR_NAME_RETENTION_DAYS,
            help="Days a name is kept after the pass first saw nothing using it "
            f"(default {settings.CREATOR_NAME_RETENTION_DAYS}).",
        )
        parser.add_argument(
            "--sources",
            default=None,
            help="Path to sources.md (default: repository root). Every name in it is kept, "
            "because `seed_sources` would re-create it.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = not options["apply"]
        which = options["which"]
        mode = " (dry run, nothing written)" if dry_run else ""
        self.stdout.write(f"research retention{mode}")
        forgotten: list[int] = []
        try:
            if which in ("queries", "all"):
                self._queries(
                    apply_query_retention(dry_run=dry_run, retain_days=options["query_days"])
                )
            if which in ("prospects", "all"):
                report = apply_prospect_retention(
                    dry_run=dry_run,
                    retain_days=options["prospect_days"],
                    sources=options["sources"],
                )
                forgotten = report.forgotten_ids
                self._prospects(report)
            if which in ("names", "all"):
                self._names(
                    apply_name_retention(
                        dry_run=dry_run,
                        retain_days=options["name_days"],
                        sources=options["sources"],
                        delete_now=forgotten,
                    )
                )
        except RetentionRefused as exc:
            raise CommandError(str(exc)) from exc

    # --- printing (ids, counts and reason codes only) -----------------------------------------

    def _queries(self, report: QueryRetentionReport) -> None:
        verb = "to scrub" if report.dry_run else "scrubbed"
        self.stdout.write(
            f"queries: {report.with_text} call(s) still carry a query text; {verb} "
            f"{report.due if report.dry_run else report.scrubbed} older than "
            f"{report.scrub_before.date().isoformat()} ({report.retain_days} days). "
            "Rows, costs and timestamps are kept: they are the spend ledger."
        )
        self.stdout.write(
            f"queries: {report.candidates_with_text} candidate(s) hold query text in their "
            f"evidence; {verb} "
            f"{report.candidates_due if report.dry_run else report.candidates_scrubbed} "
            f"untouched since {report.scrub_before.date().isoformat()}"
            + (
                f" ({report.samples_scrubbed} sample(s))"
                if not report.dry_run and report.samples_scrubbed
                else ""
            )
        )

    def _prospects(self, report: ProspectRetentionReport) -> None:
        verb = "to forget" if report.dry_run else "forgotten"
        self.stdout.write(
            f"prospects: {report.prospects} prospect(s) on the watchlist; {verb} {report.due} "
            f"added before {report.forget_before.date().isoformat()} "
            f"({report.retain_days} days): {report.sample_checks} verdict(s), "
            f"{report.page_links} page link(s), {report.breadcrumbs} breadcrumb(s)"
        )
        if report.forgotten_ids:
            self.stdout.write(f"prospects: creator id(s) {_ids(report.forgotten_ids)}")
        for pk, code in sorted(report.kept.items()):
            self.stdout.write(f"prospects: kept creator {pk} ({code})")
        coming = [
            f"{pk} due {due}" for pk, due in sorted(report.due_on.items()) if pk not in report.kept
        ]
        if coming:
            self.stdout.write(f"prospects: clocks running -- {', '.join(coming)}")

    def _names(self, report: NameRetentionReport) -> None:
        verb = "to delete" if report.dry_run else "deleted"
        self.stdout.write(
            f"names: {report.names} name(s) in the catalogue, {report.unused} used by nothing; "
            f"clocks {'to start' if report.dry_run else 'started'} {len(report.marked)}, "
            f"{'to clear' if report.dry_run else 'cleared'} {len(report.cleared)}"
        )
        self.stdout.write(
            f"names: {verb} {len(report.due_ids) if report.dry_run else len(report.deleted_ids)} "
            f"name(s) unused since before {report.delete_before.date().isoformat()} "
            f"({report.retain_days} days)"
            + (f": creator id(s) {_ids(report.due_ids)}" if report.due_ids else "")
        )
        if report.cascade:
            cascade = ", ".join(f"{name} {count}" for name, count in sorted(report.cascade.items()))
            self.stdout.write(f"names: rows that go with them -- {cascade}")
        counts = report.keep_counts
        if counts:
            kept = ", ".join(f"{code} {count}" for code, count in sorted(counts.items()))
            self.stdout.write(f"names: kept -- {kept}")


def _ids(ids: list[int], limit: int = 50) -> str:
    """Ids, never names. Long lists are cut off with a count, so the line stays readable."""
    shown = ", ".join(str(pk) for pk in ids[:limit])
    return shown if len(ids) <= limit else f"{shown}, ... (+{len(ids) - limit})"
