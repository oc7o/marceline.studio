from argparse import ArgumentParser
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from crawler.discovery.dataforseo import (
    MODE_PRICE_USD,
    DataForSEO,
    DiscoveryError,
    ProviderError,
    SpendLimit,
)
from crawler.discovery.services import (
    MARKETS,
    link_channels,
    link_domains,
    mine_channels,
    mine_links,
    plan_queries,
    reapply_rules,
    record_research,
    unsafe_queries,
    upsert,
)


class Command(BaseCommand):
    help = "Propose source domains for human review; never crawl or enable sites."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--creator", action="append", default=[])
        parser.add_argument("--query", action="append", default=[])
        parser.add_argument(
            "--market",
            choices=sorted(MARKETS),
            default="default",
            help=(
                "Search location, language and creator templates: 'default' uses "
                "DISCOVERY_LOCATION_CODE/DISCOVERY_LANGUAGE and the English templates, 'de' "
                "searches Google Germany in German with the German templates."
            ),
        )
        parser.add_argument(
            "--generic",
            action="store_true",
            help="Add the market's generic domain-discovery queries (no creator linking).",
        )
        parser.add_argument(
            "--add-domain",
            action="append",
            default=[],
            metavar="DOMAIN",
            help=(
                "Offline: record a domain named by desk research as a candidate for review "
                "(needs --note); no search call and no request to the domain."
            ),
        )
        parser.add_argument("--note", default="", help="Where --add-domain domains come from.")
        parser.add_argument("--mode", choices=("live", "standard"), default="live")
        parser.add_argument("--max-calls", type=int)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--from-links", action="store_true")
        parser.add_argument(
            "--reapply-rules",
            action="store_true",
            help=(
                "Offline: re-evaluate ignore, hoster/social and mirror rules for proposed "
                "candidates only (approved/rejected/other statuses untouched); "
                "with --dry-run, list changes without writing."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["max_calls"] is not None and options["max_calls"] < 0:
            raise CommandError("--max-calls must be nonnegative")
        if options["add_domain"]:
            self.add_domains(options["add_domain"], options["note"], dry_run=options["dry_run"])
            return
        if options["reapply_rules"]:
            if options["from_links"] or options["creator"] or options["query"]:
                raise CommandError("--reapply-rules cannot be combined with discovery options")
            changes = reapply_rules(dry_run=options["dry_run"])
            for change in changes:
                self.stdout.write(
                    f"{change.domain}: proposed -> {change.status} "
                    f"(kind {change.kind}; {change.reason})"
                )
            verb = "would change" if options["dry_run"] else "changed"
            self.stdout.write(f"calls=0 cost=$0 reapplied rules: {verb}={len(changes)}")
            return
        if options["from_links"]:
            if options["dry_run"]:
                self.stdout.write(f"Link plan: {len(link_domains())} domains; calls=0 cost=$0")
                keys = link_channels().keys()
                invites = sum(private for private, _ in keys)
                self.stdout.write(
                    f"Telegram plan: channels={len(keys) - invites} invite_links={invites} "
                    "(stored only, never crawled)"
                )
            else:
                new, updated = mine_links()
                self.stdout.write(f"calls=0 cost=$0 new={new} updated={updated}")
                counts = mine_channels()
                self.stdout.write(
                    f"Telegram candidates (never crawled): channels={counts.channels} "
                    f"(new {counts.channels_new}) invite_links={counts.invites} "
                    f"(new {counts.invites_new})"
                )
            return
        market = MARKETS[options["market"]]
        try:
            plan = plan_queries(
                options["creator"], options["query"], market=market, generic=options["generic"]
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if unsafe := unsafe_queries(plan):
            raise CommandError(
                "Refusing unsafe discovery queries: "
                + "; ".join(f"{query.text!r} ({reason})" for query, reason in unsafe)
            )
        limit = min(
            options["max_calls"]
            if options["max_calls"] is not None
            else settings.DISCOVERY_MAX_CALLS_PER_RUN,
            settings.DISCOVERY_MAX_CALLS_PER_RUN,
        )
        # Standard needs at least post + ready + get per query; polls also consume the cap.
        per_query = 1 if options["mode"] == "live" else 3
        estimate = MODE_PRICE_USD[options["mode"]]
        affordable = int(Decimal(str(settings.DISCOVERY_DAILY_BUDGET_USD)) / estimate)
        planned = len(plan)
        plan = plan[: max(0, min(limit // per_query, affordable))]
        location = market.location_code or settings.DISCOVERY_LOCATION_CODE
        language = market.language or settings.DISCOVERY_LANGUAGE
        if options["dry_run"]:
            self.stdout.write(f"market={market.code} location={location} language={language}")
            for query in plan:
                self.stdout.write(query.text)
            self.stdout.write(f"queries={len(plan)} estimated cost=${len(plan) * estimate:.6f}")
            if planned > len(plan):
                self.stdout.write(
                    f"{planned - len(plan)} more planned queries left out by the run cap/budget "
                    f"(all {planned}: estimated ${planned * estimate:.6f})"
                )
            return
        try:
            client = DataForSEO(options["mode"], limit, location_code=location, language=language)
        except DiscoveryError as exc:
            raise CommandError(str(exc)) from exc
        new_domains: set[str] = set()
        updated_domains: set[str] = set()
        for query in plan:
            try:
                items = client.search(query.text)
            except ProviderError as exc:
                self.stderr.write(str(exc))
                if exc.stops_run:
                    break
                continue
            except (SpendLimit, DiscoveryError) as exc:
                self.stderr.write(str(exc))
                break
            for item in items:
                candidate, created = upsert(
                    item["url"], query=query, rank=item["rank"], title=item["title"]
                )
                if candidate:
                    (new_domains if created else updated_domains).add(candidate.domain)
        self.stdout.write(
            f"calls={client.calls} cost=${client.cost:.6f} new={len(new_domains)} "
            f"updated={len(updated_domains - new_domains)}"
        )

    def add_domains(self, domains: list[str], note: str, *, dry_run: bool) -> None:
        if not note.strip():
            raise CommandError("--add-domain needs --note (where the domain comes from)")
        if dry_run:
            for domain in domains:
                self.stdout.write(f"would record {domain}")
            self.stdout.write(f"calls=0 cost=$0 research domains={len(domains)} (dry run)")
            return
        new = updated = skipped = 0
        for domain in domains:
            result = record_research(domain, note)
            if result.candidate is None:
                skipped += 1
                self.stdout.write(f"{result.domain}: skipped ({result.skipped})")
                continue
            new, updated = new + int(result.created), updated + int(not result.created)
            self.stdout.write(
                f"{result.domain}: {'new' if result.created else 'updated'} "
                f"(status {result.candidate.status}, kind {result.candidate.kind})"
            )
        self.stdout.write(f"calls=0 cost=$0 new={new} updated={updated} skipped={skipped}")
