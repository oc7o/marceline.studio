import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from catalog.models import Creator, WatchlistEntry
from marceline.prospects import ResearchBasis, prospect_report, render_briefing, render_text


class Command(BaseCommand):
    help = (
        "Prospect research (docs/prospect-research.md), staff only: per prospect, attributed pages "
        "by strength and site, distinct videos (clusters) and photo sets, copies, sites, hosters, "
        "jurisdictions, share online, the claim with its gate, and a seeded verification sample of "
        "page ids with recorded verdicts. A number is refused unless every gate passes and is "
        "never formatted without its 'as of' date and the measured sample precision. Names and PDQ "
        "clusters only, never biometrics; no media, titles or URLs in the output. Writes nothing. "
        "--briefing renders the operator-facing outreach briefing instead: counts only, no page "
        "ids and no domains."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--creator", action="append", dest="creators", help="This creator (repeatable)."
        )
        parser.add_argument(
            "--all-prospects",
            action="store_true",
            help="Every watchlist entry with purpose=prospect, crawled or not.",
        )
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument(
            "--seed", type=int, default=0, help="Verification sample seed (default 0)."
        )
        parser.add_argument(
            "--briefing",
            action="store_true",
            help="Render the outreach briefing (docs/prospect-research.md) instead of the "
            "research report. Counts only: no page ids, no site domains, no hosters.",
        )
        parser.add_argument(
            "--basis",
            choices=(ResearchBasis.LEGAL_PERSON.value, ResearchBasis.EXPLICIT_OPT_IN.value),
            default=ResearchBasis.NOT_STATED.value,
            help="Why we may hold this research: legal_person (a company, researched by brand and "
            "title) or explicit_opt_in (they asked for the check; record scope and date). There "
            "is deliberately no default: without it no number may be stated at all.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        names: list[str] = options["creators"] or []
        if not names and not options["all_prospects"]:
            raise CommandError("name --creator NAME or --all-prospects")
        creators = list(Creator.objects.filter(name__in=names))
        missing = sorted(set(names) - {creator.name for creator in creators})
        if missing:
            raise CommandError(f"unknown creator(s): {', '.join(missing)}")
        if options["all_prospects"]:
            creators += [
                entry.creator
                for entry in WatchlistEntry.objects.filter(
                    purpose=WatchlistEntry.Purpose.PROSPECT
                ).select_related("creator")
            ]
        unique = sorted({creator.pk: creator for creator in creators}.values(), key=str)
        basis = ResearchBasis(options["basis"])
        try:
            # A suppressed name has no report (docs/suppression.md): the refusal is loud, because
            # a silently missing prospect would look like "nothing found" to whoever asked.
            reports = [
                prospect_report(creator, seed=options["seed"], basis=basis) for creator in unique
            ]
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if options["format"] == "json":
            payload = [report.as_json() for report in reports]
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            return
        if not reports:
            self.stdout.write("No prospects.")
        render = render_briefing if options["briefing"] else render_text
        for report in reports:
            for line in render(report):
                self.stdout.write(line)
