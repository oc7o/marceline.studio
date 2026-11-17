"""Targeted indexing (issue #71): whom we hunt, at what priority, and which pages stay active.

**Targets** are the performers of originals an organisation owns (the *Auftrag*). Until real
contracts exist, "owned" is exactly what `marceline.app.ownership` says: the original's
organisation column (ADR 0031). Unassigned originals (operator/CLI imports nobody assigned) own
nothing and make nobody a target. Operator decisions of 2026-09-17:
- a **rights holder** (the primary creator, `Reference.creator`, of any owned original) is hunted
  at `SEED_PRIORITY`;
- a **co-performer** (only ever in `Reference.creators`, never primary) is hunted too, below every
  rights holder, the higher the more prominent: `co_performer_priority`;
- the **watchlist** (`catalog.WatchlistEntry`) is *not* crawled — it notes interest. Only an entry
  with `crawl` ticked becomes a target, at `WATCHLIST_SEED_PRIORITY`;
- a crawled watchlist entry with `purpose=prospect` is **prospect research** (2026-09-17,
  docs/prospect-research.md): the lowest target priority, `PROSPECT_SEED_PRIORITY`, and at most
  `PROSPECT_DAILY_SEED_CAP` seed tasks per prospect in any rolling 24 hours (`prospect_seed_plan`).
  Its pages stay active like any target's.

**Active pages.** A page stays (or becomes) active when any of these holds; otherwise it is
deactivated with reason `untargeted` — never deleted:
- `linked`: `Page.creators` names a target. The crawler links a creator when their name or an
  alias appears as whole words in the page's title, tags or performer credits
  (`crawler.services.match_creators`);
- `matched`: a `ReferenceMatch` or `ReferenceLeak` row (any status) ties the page to an original
  of a target;
- `clustered`: the page is a non-rejected member of a cluster that has a non-rejected `linked` or
  `matched` member;
- `seeded`: the page's detail task was discovered from a target's seed task, directly or through
  that seed's listing/search pagination (never through another detail page);
- `safety`: the page is not safety-clear. Flagged pages keep their existing handling (ADR 0018,
  incidents): their media is gated anyway, and deactivation must not stop their rechecks.

It lives in the project package because it needs `accounts`, `matching`, `crawler` and `index`
together, which no app may import (like `marceline.scheduler`).
"""

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, QuerySet
from django.db.models.expressions import RawSQL
from django.utils import timezone

from accounts.models import Organisation
from catalog.models import Creator, Site, WatchlistEntry
from crawler.models import CrawlTask
from crawler.services import normalize_url
from crawler.sites.base import (
    PROSPECT_SEED_PRIORITY,
    SEED_PRIORITY,
    WATCHLIST_SEED_PRIORITY,
    TaskSpec,
)
from crawler.sites.registry import get_adapter
from index.breadcrumbs import rebuild_breadcrumbs
from index.models import FrameHash, Page
from index.retention import scrub_deactivated_pages
from marceline.app.ownership import owned_references
from matching.models import ClusterMember, Reference, ReferenceLeak, ReferenceMatch


class TargetKind(StrEnum):
    RIGHTS_HOLDER = "rights_holder"
    CO_PERFORMER = "co_performer"
    WATCHLIST = "watchlist"  # only entries with `crawl` ticked
    PROSPECT = "prospect"  # crawled watchlist entries with purpose=prospect


# Co-performer seed priority (operator decision 2026-09-17). A one-off co-actor starts at the
# floor; every further owned original adds a step, every further distinct target they co-appear
# with a smaller one (capped, so a crowded cast cannot outrank repeated appearances); the ceiling
# stays below every rights holder.
CO_PERFORMER_MIN_PRIORITY = 12
CO_PERFORMER_MAX_PRIORITY = SEED_PRIORITY - 2
CO_PERFORMER_PER_EXTRA_ORIGINAL = 3
CO_PERFORMER_PER_EXTRA_PARTNER = 1
CO_PERFORMER_MAX_PARTNER_BONUS = 3


def co_performer_priority(originals: int, partners: int) -> int:
    """Seed priority of a co-performer in `originals` owned originals, who appears alongside
    `partners` distinct other targets there. Pure; always within the co-performer band."""
    extra_originals = max(originals - 1, 0) * CO_PERFORMER_PER_EXTRA_ORIGINAL
    extra_partners = min(
        max(partners - 1, 0) * CO_PERFORMER_PER_EXTRA_PARTNER, CO_PERFORMER_MAX_PARTNER_BONUS
    )
    return min(
        CO_PERFORMER_MIN_PRIORITY + extra_originals + extra_partners, CO_PERFORMER_MAX_PRIORITY
    )


@dataclass(frozen=True)
class Target:
    creator: Creator
    kind: TargetKind
    priority: int
    originals: int = 0  # owned originals the creator performs in
    partners: int = 0  # distinct other performers of those originals


def owned_casts() -> list[tuple[int, set[int]]]:
    """(primary creator id, every performer id) per owned original."""
    casts: dict[int, tuple[int, set[int]]] = {}
    for organisation in Organisation.objects.order_by("pk"):
        owned = owned_references(organisation)
        for pk, primary in owned.values_list("pk", "creator_id"):
            casts[pk] = (primary, {primary})
        for pk, creator_id in Reference.creators.through.objects.filter(
            reference__in=owned
        ).values_list("reference_id", "creator_id"):
            casts[pk][1].add(creator_id)
    return [casts[pk] for pk in sorted(casts)]


def targets() -> list[Target]:
    """Whom we hunt, highest priority first (then by name): rights holders, co-performers by
    prominence, then watchlist entries marked `crawl`. Each creator appears once, in its best role.
    """
    rights_holders: set[int] = set()
    originals: dict[int, int] = defaultdict(int)
    partners: dict[int, set[int]] = defaultdict(set)
    for primary, cast in owned_casts():
        rights_holders.add(primary)
        for creator_id in cast:
            originals[creator_id] += 1
            partners[creator_id] |= cast - {creator_id}
    crawled_watchlist = dict(
        WatchlistEntry.objects.filter(crawl=True).values_list("creator_id", "purpose")
    )
    ids = set(originals) | set(crawled_watchlist)
    hunted: list[Target] = []
    # `usable()` decides who may be hunted at all: a name a customer typed is hunted only once
    # staff reviewed it (`Creator.needs_review`, issue #29) — an upload is a claim, and a claim
    # does not get to steer the crawler — and a suppressed name (docs/suppression.md) is hunted
    # never, whatever an original's cast or the watchlist says.
    for creator in Creator.objects.usable().filter(pk__in=ids):
        pk = creator.pk
        if pk in rights_holders:
            kind, priority = TargetKind.RIGHTS_HOLDER, SEED_PRIORITY
        elif pk in originals:
            kind = TargetKind.CO_PERFORMER
            priority = co_performer_priority(originals[pk], len(partners[pk]))
        elif crawled_watchlist[pk] == WatchlistEntry.Purpose.PROSPECT:
            kind, priority = TargetKind.PROSPECT, PROSPECT_SEED_PRIORITY
        else:
            kind, priority = TargetKind.WATCHLIST, WATCHLIST_SEED_PRIORITY
        hunted.append(Target(creator, kind, priority, originals[pk], len(partners[pk])))
    # Prospects list last among equal priorities: research is the lowest target.
    return sorted(
        hunted,
        key=lambda target: (
            -target.priority,
            target.kind == TargetKind.PROSPECT,
            target.creator.name,
        ),
    )


def uncrawled_watchlist(hunted: list[Target] | None = None) -> list[Creator]:
    """Watchlist creators we only note (no `crawl`, no Auftrag): no seeds, no active pages."""
    hunted_ids = {target.creator.pk for target in (targets() if hunted is None else hunted)}
    return [
        entry.creator
        for entry in WatchlistEntry.objects.select_related("creator").order_by("creator__name")
        if entry.creator_id not in hunted_ids
    ]


def seed_specs(site: Site, hunted: list[Target]) -> list[TaskSpec]:
    """The site adapter's creator seeds for these targets, each at its target's priority."""
    adapter = get_adapter(site.domain)
    if adapter is None:
        return []
    by_priority: dict[int, list[Creator]] = defaultdict(list)
    for target in hunted:
        by_priority[target.priority].append(target.creator)
    return [
        replace(spec, priority=priority)
        for priority, creators in sorted(by_priority.items(), reverse=True)
        for spec in adapter.seed_tasks(site, creators)
    ]


def _seed_used(task: CrawlTask | None, since: datetime) -> bool:
    """A seed that counts against a prospect's daily cap: queued, running, or fetched lately."""
    if task is None:
        return False
    if task.status in (CrawlTask.Status.PENDING, CrawlTask.Status.RUNNING):
        return True
    return task.fetched_at is not None and task.fetched_at >= since


def prospect_seed_plan(
    target: Target, *, now: datetime, cap: int | None = None
) -> list[tuple[Site, TaskSpec]]:
    """Which of a prospect's seeds may be queued now, on any enabled site.

    At most `cap` (default `PROSPECT_DAILY_SEED_CAP`) of the prospect's seed tasks may be queued,
    running or fetched within the last 24 hours, across all sites; the rest of the allowance goes to
    the seeds fetched longest ago (never fetched first), in site and adapter order otherwise. The
    plan is a pure function of the queue, so the scheduler's one-site-at-a-time `crawl_seed` calls
    agree on it and each site enqueues its own share.
    """
    cap = settings.PROSPECT_DAILY_SEED_CAP if cap is None else cap
    candidates: dict[str, tuple[Site, TaskSpec]] = {}
    for site in Site.objects.filter(enabled=True).order_by("domain"):
        adapter = get_adapter(site.domain)
        if adapter is None:
            continue
        for spec in adapter.seed_tasks(site, [target.creator]):
            url = normalize_url(spec.url)
            candidates.setdefault(url, (site, replace(spec, priority=target.priority)))
    tasks = {task.url: task for task in CrawlTask.objects.filter(url__in=list(candidates))}
    since = now - timedelta(hours=24)
    used = sum(_seed_used(tasks.get(url), since) for url in candidates)
    order = {url: position for position, url in enumerate(candidates)}
    fresh = [url for url in candidates if not _seed_used(tasks.get(url), since)]

    def least_recent(url: str) -> tuple[bool, datetime, int]:
        fetched = tasks[url].fetched_at if url in tasks else None
        return (fetched is not None, fetched or now, order[url])

    fresh.sort(key=least_recent)
    return [candidates[url] for url in fresh[: max(cap - used, 0)]]


def target_seed_urls(hunted: list[Target]) -> list[str]:
    """Seed URLs of every target on every site with an adapter, enabled or not."""
    urls = {
        normalize_url(spec.url)
        for site in Site.objects.order_by("pk")
        for spec in seed_specs(site, hunted)
    }
    return sorted(urls)


def _reach_sql(select: str) -> str:
    """Tasks reachable from the tasks at the URLs given as the only parameter, following
    `discovered_from` through listing and search tasks, never out of a detail task; `select` reads
    the `reach(id, kind)` result."""
    task = CrawlTask._meta.db_table
    detail = CrawlTask.Kind.DETAIL.value
    # Table names and a kind constant only; the URLs are a query parameter. UNION (not UNION ALL)
    # terminates on cycles: `enqueue` rewrites discovered_from of pending detail tasks, so a
    # refreshed task can end up pointing back at its own descendant.
    return f"""
        WITH RECURSIVE reach(id, kind) AS (
            SELECT id, kind FROM {task} WHERE url = ANY(%s)
            UNION
            SELECT child.id, child.kind FROM {task} child
            JOIN reach ON child.discovered_from_id = reach.id
            WHERE reach.kind <> '{detail}'
        )
        {select}
    """


def _seeded_pages_sql() -> str:
    task = CrawlTask._meta.db_table
    page = Page._meta.db_table
    detail = CrawlTask.Kind.DETAIL.value
    return _reach_sql(
        f"SELECT p.id FROM {page} p JOIN {task} t ON t.url = p.url "
        f"JOIN reach ON reach.id = t.id WHERE reach.kind = '{detail}'"
    )


def broad_listing_urls() -> list[str]:
    """Every site's `listing_tasks` start URLs (newest uploads, whole catalogues)."""
    urls: set[str] = set()
    for site in Site.objects.order_by("pk"):
        adapter = get_adapter(site.domain)
        if adapter is not None:
            urls.update(normalize_url(spec.url) for spec in adapter.listing_tasks(site))
    return sorted(urls)


def pending_broad_listings() -> QuerySet[CrawlTask]:
    """Pending listing/search tasks of a broad listing's pagination chain.

    Before targeted indexing these were seeded daily; a chain already in the queue would keep
    paginating through a whole catalogue. Pending detail tasks are left to finish: they are
    bounded, and their pages still get the active-page rule.
    """
    urls = broad_listing_urls()
    if not urls:
        return CrawlTask.objects.none()
    return (
        CrawlTask.objects.filter(status=CrawlTask.Status.PENDING)
        .exclude(kind=CrawlTask.Kind.DETAIL)
        .filter(pk__in=RawSQL(_reach_sql("SELECT id FROM reach"), [urls]))
    )


BROAD_LISTING_STOPPED = "broad listing stopped: targeted indexing (issue #71)"


def stop_broad_listings() -> int:
    """Skip pending broad-listing chains; `crawl_seed --listings --refresh` re-queues them."""
    return pending_broad_listings().update(
        status=CrawlTask.Status.SKIPPED,
        decision_reason=BROAD_LISTING_STOPPED,
        locked_at=None,
        updated_at=timezone.now(),
    )


@dataclass(frozen=True)
class KeepRules:
    """The active-page rule as named conditions over `Page`, for filtering and counting."""

    linked: Q
    matched: Q
    clustered: Q
    seeded: Q
    safety: Q

    @property
    def any(self) -> Q:
        return self.linked | self.matched | self.clustered | self.seeded | self.safety

    def named(self) -> dict[str, Q]:
        return {
            "linked": self.linked,
            "matched": self.matched,
            "clustered": self.clustered,
            "seeded": self.seeded,
            "safety": self.safety,
        }


def keep_rules(hunted: list[Target]) -> KeepRules:
    creator_ids = sorted({target.creator.pk for target in hunted})
    target_references = Reference.objects.filter(
        Q(creator_id__in=creator_ids) | Q(creators__in=creator_ids)
    ).values("pk")
    linked = Exists(
        Page.creators.through.objects.filter(page_id=OuterRef("pk"), creator_id__in=creator_ids)
    )
    matched = Exists(
        ReferenceMatch.objects.filter(page_id=OuterRef("pk"), reference_id__in=target_references)
    ) | Exists(
        ReferenceLeak.objects.filter(page_id=OuterRef("pk"), reference_id__in=target_references)
    )
    members = ClusterMember.objects.exclude(status=ClusterMember.Status.REJECTED)
    anchors = Page.objects.filter(Q(linked) | Q(matched)).values("pk")
    clustered = Exists(
        members.filter(
            page_id=OuterRef("pk"),
            cluster_id__in=members.filter(page_id__in=anchors).values("cluster_id"),
        )
    )
    seed_urls = target_seed_urls(hunted)
    seeded = Q(pk__in=RawSQL(_seeded_pages_sql(), [seed_urls])) if seed_urls else Q(pk__in=[])
    return KeepRules(
        linked=Q(linked),
        matched=Q(matched),
        clustered=Q(clustered),
        seeded=seeded,
        safety=~Q(safety_status=Page.SafetyStatus.CLEAR),
    )


@dataclass
class SiteActivity:
    domain: str
    pages: int = 0
    active: int = 0
    keep: int = 0
    deactivate: int = 0
    reactivate: int = 0
    frames: int = 0
    frames_keep: int = 0
    frames_deactivate: int = 0


@dataclass
class ActivityPlan:
    targets: list[Target]
    sites: list[SiteActivity]
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> SiteActivity:
        total = SiteActivity("total")
        for row in self.sites:
            for name in (
                "pages",
                "active",
                "keep",
                "deactivate",
                "reactivate",
                "frames",
                "frames_keep",
                "frames_deactivate",
            ):
                setattr(total, name, getattr(total, name) + getattr(row, name))
        return total


def _untargeted() -> Q:
    return Q(active=False, inactive_reason=Page.InactiveReason.UNTARGETED)


def plan_activity(hunted: list[Target] | None = None) -> ActivityPlan:
    """Read-only: per site, how many pages and frame hashes stay active and what would change."""
    hunted = targets() if hunted is None else hunted
    rules = keep_rules(hunted)
    keep = rules.any
    rows: dict[str, SiteActivity] = {}
    # Annotation names must not shadow fields (`active`) the filters refer to.
    for row in Page.objects.values("site__domain").annotate(
        n_pages=Count("pk"),
        n_active=Count("pk", filter=Q(active=True)),
        n_keep=Count("pk", filter=keep),
        n_deactivate=Count("pk", filter=Q(active=True) & ~keep),
        n_reactivate=Count("pk", filter=_untargeted() & keep),
    ):
        rows[row["site__domain"]] = SiteActivity(
            row["site__domain"],
            pages=row["n_pages"],
            active=row["n_active"],
            keep=row["n_keep"],
            deactivate=row["n_deactivate"],
            reactivate=row["n_reactivate"],
        )
    kept_pages = Page.objects.filter(keep).values("pk")
    for frames in FrameHash.objects.values("media__page__site__domain").annotate(
        frames=Count("pk"),
        frames_keep=Count("pk", filter=Q(media__page__in=kept_pages)),
        frames_deactivate=Count(
            "pk", filter=Q(media__page__active=True) & ~Q(media__page__in=kept_pages)
        ),
    ):
        site = rows[frames["media__page__site__domain"]]
        site.frames = frames["frames"]
        site.frames_keep = frames["frames_keep"]
        site.frames_deactivate = frames["frames_deactivate"]
    reasons = Page.objects.aggregate(
        **{name: Count("pk", filter=condition) for name, condition in rules.named().items()}
    )
    return ActivityPlan(hunted, [rows[domain] for domain in sorted(rows)], reasons)


class NoTargetsError(RuntimeError):
    """Refused: with no target at all, applying would deactivate every clear page."""


@dataclass(frozen=True)
class ActivityResult:
    deactivated: int
    reactivated: int
    breadcrumbs: int


@transaction.atomic
def apply_activity(hunted: list[Target] | None = None) -> ActivityResult:
    """Deactivate untargeted pages, reactivate pages that belong to targets again, rebuild the
    breadcrumbs. Idempotent. Only pages this rule deactivated are ever reactivated by it.

    A newly deactivated page is scrubbed of what is about people (`index.retention`): title, tags
    and performer credits go, while its URL, timestamps, site, hashes and `Page.creators` stay
    (the last one is what lets this same rule find and reactivate it later; it also drops out of
    the site x creator breadcrumbs the moment it deactivates, since those count active pages
    only — `index.breadcrumbs`). `deactivated_at` starts its retention clock (`page_retention`,
    ADR 0029 amendment 2026-09-17) and is cleared the moment it reactivates.
    """
    hunted = targets() if hunted is None else hunted
    if not hunted:
        raise NoTargetsError("no targets (no owned originals, empty watchlist): nothing applied")
    keep = keep_rules(hunted).any
    now = timezone.now()
    newly_deactivated = list(
        Page.objects.filter(active=True).exclude(keep).values_list("pk", flat=True)
    )
    deactivated = Page.objects.filter(pk__in=newly_deactivated).update(
        active=False,
        inactive_reason=Page.InactiveReason.UNTARGETED,
        active_changed_at=now,
        deactivated_at=now,
    )
    scrub_deactivated_pages(newly_deactivated)
    reactivated = (
        Page.objects.filter(_untargeted())
        .filter(keep)
        .update(active=True, inactive_reason="", active_changed_at=now, deactivated_at=None)
    )
    return ActivityResult(deactivated, reactivated, rebuild_breadcrumbs())


def group_targets(hunted: list[Target]) -> dict[TargetKind, list[str]]:
    """Names per kind, co-performers with their priority, for reports."""
    names: dict[TargetKind, list[str]] = defaultdict(list)
    for target in hunted:
        label = target.creator.name
        if target.kind == TargetKind.CO_PERFORMER:
            label += f" (p{target.priority}, {target.originals} originals)"
        names[target.kind].append(label)
    return names
