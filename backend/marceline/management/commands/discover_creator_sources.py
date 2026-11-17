"""Plan creator-source discovery queries for target creators only (issue #118).

It lives in the project package because it needs `marceline.targets` — whom we were actually
asked to look for — and `crawler` may not import it (module boundaries, root `CLAUDE.md`).

**It makes no request and writes nothing.** It prints the plan, the safety verdict and what the
queries would cost, so the operator can decide whether to spend anything at all. Executing them is
a separate, deliberate step through `discover_sources`, and that step does not exist for these
templates yet on purpose (see `docs/research/creator-leak-sources.md`, decision sheet).
"""

from argparse import ArgumentParser
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from crawler.discovery.dataforseo import MODE_PRICE_USD
from crawler.discovery.services import (
    CREATOR_MARKET,
    GERMAN_CREATOR_MARKET,
    plan_queries,
    unsafe_queries,
)
from marceline.targets import TargetKind, targets

MARKETS = {market.code: market for market in (CREATOR_MARKET, GERMAN_CREATOR_MARKET)}
#: Prospects are research before opt-in (docs/prospect-research.md). Spending a search on a
#: prospect's name in a leak vocabulary is not research we may do before they asked us to.
SEEDABLE_KINDS = frozenset(
    {TargetKind.RIGHTS_HOLDER, TargetKind.CO_PERFORMER, TargetKind.WATCHLIST}
)


class Command(BaseCommand):
    help = (
        "Plan creator-source discovery queries for target creators; no request, no write, "
        "no paid call."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--market",
            choices=sorted(MARKETS),
            default="creator",
            help="'creator' = English templates, 'creator-de' = German, Google Germany.",
        )
        parser.add_argument(
            "--creator",
            action="append",
            default=[],
            help="Restrict to these creators; a name that is not a target plans nothing.",
        )
        parser.add_argument("--mode", choices=("live", "standard"), default="live")

    def handle(self, *args: Any, **options: Any) -> None:
        market = MARKETS[options["market"]]
        hunted = [target for target in targets() if target.kind in SEEDABLE_KINDS]
        skipped = [target for target in targets() if target.kind not in SEEDABLE_KINDS]
        target_names = frozenset(target.creator.name for target in hunted)
        if not target_names:
            raise CommandError(
                "No target creators: nobody has asked us to look yet "
                "(marceline/targets.py, docs/research/creator-leak-sources.md)"
            )
        try:
            plan = plan_queries(options["creator"], [], market=market, target_names=target_names)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if unsafe := unsafe_queries(plan):
            raise CommandError(
                "Refusing unsafe discovery queries (docs/research/german-market.md §4): "
                + "; ".join(f"{query.text!r} ({reason})" for query, reason in unsafe)
            )
        location = market.location_code or settings.DISCOVERY_LOCATION_CODE
        language = market.language or settings.DISCOVERY_LANGUAGE
        self.stdout.write(
            f"market={market.code} location={location} language={language} "
            f"targets={len(hunted)} templates={len(market.templates)}"
        )
        for query in plan:
            self.stdout.write(query.text)
        estimate = MODE_PRICE_USD[options["mode"]]
        cap = settings.DISCOVERY_MAX_CALLS_PER_RUN
        affordable = int(Decimal(str(settings.DISCOVERY_DAILY_BUDGET_USD)) / estimate)
        self.stdout.write(
            f"queries={len(plan)} estimated cost=${len(plan) * estimate:.6f} "
            f"(a run is capped at {cap} calls and {affordable} queries by the daily budget)"
        )
        if skipped:
            self.stdout.write(
                f"{len(skipped)} prospect(s) left out: research before opt-in never spends a "
                "search on a name (docs/prospect-research.md)"
            )
        self.stdout.write("calls=0 cost=$0 — plan only; nothing was requested and nothing written")
