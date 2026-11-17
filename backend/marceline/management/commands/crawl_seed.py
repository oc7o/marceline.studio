from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from catalog.models import Creator, Site
from catalog.suppression import active_suppressions, suppressed_names
from crawler import services
from crawler.sites.base import SEED_PRIORITY, TaskSpec, has_card_text
from crawler.sites.common import creator_names, name_keys, names_person
from crawler.sites.registry import get_adapter
from marceline.targets import Target, TargetKind, prospect_seed_plan, seed_specs, targets


class Command(BaseCommand):
    help = (
        "Enqueue seed crawl tasks for enabled sites x target creators (issue #71): rights holders "
        "of owned originals first, co-performers below them by prominence, then watchlist "
        "entries marked crawl, prospects last and capped per day (PROSPECT_DAILY_SEED_CAP). "
        "Uncrawled watchlist entries get no seeds."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", help="Only this domain.")
        parser.add_argument(
            "--creator",
            action="append",
            dest="creators",
            help="This creator instead of the targets, target or not (repeatable).",
        )
        parser.add_argument("--max-creators", type=int, help="Use at most N creators.")
        parser.add_argument(
            "--query",
            action="append",
            dest="queries",
            help="Free-text search seed, e.g. a video title (repeatable); replaces creator seeds.",
        )
        parser.add_argument(
            "--listings",
            action="store_true",
            help=(
                "Also enqueue each site's broad newest/catalogue listings (about no creator); "
                "a site whose adapter declares `card_text` walks them breadth first behind a "
                "title gate and resumes a stalled walk where it stopped (ADR 0029 amendment)."
            ),
        )
        parser.add_argument(
            "--refresh", action="store_true", help="Re-queue seed tasks that already ran."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        sites = Site.objects.filter(enabled=True)
        if options["site"]:
            sites = sites.filter(domain=options["site"])
        queries: list[str] = options["queries"] or []
        if queries and (hit := _suppressed_in(queries)):
            # A free-text seed is a crawl aimed at whatever it names (docs/suppression.md).
            raise CommandError("query names a suppressed person, not seeded: " + ", ".join(hit))
        hunted: list[Target] = []
        if not queries:
            if options["creators"]:
                # A manual override: named creators seed at rights-holder priority, target or not
                # — except a suppressed name, which no override may hunt (docs/suppression.md).
                suppressed = suppressed_names(options["creators"])
                if suppressed:
                    raise CommandError(
                        "suppressed, never seeded (docs/suppression.md): " + ", ".join(suppressed)
                    )
                named = Creator.objects.filter(name__in=options["creators"]).order_by("name")
                hunted = [
                    Target(creator, TargetKind.RIGHTS_HOLDER, SEED_PRIORITY) for creator in named
                ]
            else:
                hunted = targets()
            if options["max_creators"] is not None:
                hunted = hunted[: options["max_creators"]]
        # Prospect research is capped per prospect per day across all sites (not a manual override).
        prospects = [target for target in hunted if target.kind == TargetKind.PROSPECT]
        hunted_now = [target for target in hunted if target.kind != TargetKind.PROSPECT]
        now = timezone.now()
        prospect_plans = [prospect_seed_plan(target, now=now) for target in prospects]
        scope = (
            f"{len(queries)} query(ies)"
            if queries
            else f"{len(hunted)} creator(s)" + _breakdown(hunted, manual=bool(options["creators"]))
        )

        for site in sites:
            adapter = get_adapter(site.domain)
            if adapter is None:
                self.stdout.write(f"{site.domain}: no adapter yet, skipped")
                continue
            specs: list[TaskSpec]
            if queries:
                specs = [task for query in queries for task in adapter.search_tasks(site, query)]
            else:
                specs = seed_specs(site, hunted_now) + [
                    spec for plan in prospect_plans for seed_site, spec in plan if seed_site == site
                ]
            walk_specs: list[TaskSpec] = []
            if options["listings"]:
                listings = adapter.listing_tasks(site)
                # A site whose cards carry their title is walked breadth first with the gate;
                # the others keep the plain chain they always had (there is nothing to gate on).
                if has_card_text(adapter):
                    walk_specs = listings
                else:
                    specs.extend(listings)
            created = services.enqueue_seeds(site, specs, refresh=options["refresh"])
            suffix = " + broad listings" if options["listings"] else ""
            self.stdout.write(f"{site.domain}: {created} new task(s) for {scope}{suffix}")
            if walk_specs:
                walks = services.enqueue_listing_walks(site, walk_specs, refresh=options["refresh"])
                self.stdout.write(f"{site.domain}: {_walks(walks)}")


def _walks(status: services.WalkStatus) -> str:
    """One line per site about its gated catalogue walks (`crawler.listing_walk`)."""
    parts = [
        f"{name} {', '.join(urls)}"
        for name, urls in (
            ("started", status.started),
            ("resumed at", status.resumed),
            ("walking at", status.walking),
            ("complete at", status.complete),
        )
        if urls
    ]
    return "listing walk(s): " + ("; ".join(parts) if parts else "none")


def _suppressed_in(queries: list[str]) -> list[str]:
    """Suppressed names any of these free-text queries names (docs/suppression.md)."""
    named: set[str] = set()
    for suppression in active_suppressions():
        keys = name_keys(creator_names(suppression.creator))
        if any(names_person(query, keys) for query in queries):
            named.add(suppression.creator.name)
    return sorted(named)


def _breakdown(hunted: list[Target], *, manual: bool) -> str:
    if manual:
        return ""
    counts = {kind: sum(target.kind == kind for target in hunted) for kind in TargetKind}
    return (
        f" ({counts[TargetKind.RIGHTS_HOLDER]} rights holder(s), "
        f"{counts[TargetKind.CO_PERFORMER]} co-performer(s), "
        f"{counts[TargetKind.WATCHLIST]} crawled watchlist, "
        f"{counts[TargetKind.PROSPECT]} prospect(s))"
    )
