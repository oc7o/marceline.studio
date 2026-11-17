"""Is this site's adapter still parsing? (issue #22, ADR 0042)

A hand-written adapter does not crash when a site changes its markup. It keeps returning, and what
it returns is empty: a page stored with no title, no duration, no performer credits, no media — or,
most often, no page at all, because the selector that found the video container found nothing. The
crawl budget is spent either way, so the break has to be *noticed*, not discovered weeks later.

**The signal is the share of detail parses that come back blind**: no page, or a page with no
title, or a page with no media. Those three are the fields that every one of the seventeen live
adapters fills, essentially always.

**Measured on the development database, 2026-09-18**, over the 38,110 active, safety-clear pages
(inactive pages are excluded: `index.retention.scrub_deactivated_pages` blanks their title, which
would look exactly like a break):

===================  ==============================================
empty title          0 pages of 38,110 — 0.00 %, on every one of the 17 sites
no media at all      10 pages of 38,110 — 0.026 % (8 on one site, 2 on another)
missing duration     0 % or 100 % per site — three sites simply never publish one
missing performers   0 % … 100 % per site, and 0.8 % → 16.9 % *day to day* on one site
===================  ==============================================

So title and media are near-binary and comparable across sites, while duration and performers are
not: the day-to-day swing of the performer rate on a healthy adapter (site19 0.8 % → 16.9 %,
site03 0.4 % → 10.5 %, site08 10.3 % → 22.6 %, all without any adapter change) is larger than most
breaks would be. **Duration and performers are therefore counted and reported, and never alarmed
on.** A threshold on them would cry wolf several times a week.

`BROKEN_SHARE` is 0.30 against a measured normal of 0.026 %: more than a thousandfold above the
observed rate, which leaves room for the one thing the stored pages cannot measure — how often
`parse()` legitimately returns nothing for a fetched detail URL — without needing that number to be
small. `RECOVERED_SHARE` (0.10) is the hysteresis: a site that has been called broken stays broken
until it is clearly working again, so a parser wobbling around the threshold does not mail the
operator twice an hour.

**Not every site's detail page carries media** (amendment 2026-09-19). Three adapters
(`site04`, `site02`, `site07`) parse video pages that show no image of their own — the player
is a script and the related cards belong to other videos — so every poster and frame travels with
the task from the card that linked it (`TaskSpec.media`). Their detail parses return
`ParseResult.media == ()` *while working perfectly*, which the first version of this module counted
as blind: on 2026-09-19 two of them were reported 100 % broken (14,212 of 14,212 and 2,661 of 2,661
parses) while 11,758 of 11,903 and 2,337 of 2,340 pages seen in the same 24 h had media and not one
active, safety-clear page on either site had none. So the media half of the rule is asked of the
adapter first (`crawler.sites.base.has_card_media_only`); for such a site blind means no page or no
title, and `no_media` is context only, like duration and performers.

Everything above the database line is pure.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import connection
from django.utils import timezone

from catalog.models import Site
from crawler.models import AdapterHealth, AdapterParseStat
from crawler.sites.base import ParseResult, has_card_media_only
from crawler.sites.registry import get_adapter

#: A detail parse yields nothing usable this often → the adapter is broken. Measured normal:
#: 0.026 % (10 pages of 38,110). See the module docstring.
BROKEN_SHARE = 0.30
#: …and a broken adapter is only called repaired below this. Hysteresis, not a second threshold.
RECOVERED_SHARE = 0.10
#: Below this many detail parses in the window the verdict is `unknown`, never `ok`: a quiet site
#: must not be able to report health it has not demonstrated.
MIN_PARSES = 25
#: The window a verdict is computed over, and the older window it is compared against.
WINDOW_HOURS = 24
BASELINE_HOURS = 7 * 24


@dataclass(frozen=True)
class ParseOutcome:
    """What one detail parse yielded. Booleans only — never a URL, a title or any page text."""

    no_page: bool
    no_title: bool = False
    no_media: bool = False
    no_duration: bool = False
    no_performers: bool = False
    #: Does an empty media tuple mean anything on this site? False for an adapter that declares
    #: `card_media_only` (see the module docstring): its detail pages never carry media, so
    #: `no_media` is context there and never part of `blind`. Not stored — the reader asks the
    #: adapter again (`evaluate`), so one flag can never be recorded two ways.
    media_counts: bool = True

    @property
    def blind(self) -> bool:
        """Did this parse produce nothing an indexer can work with?"""
        return self.no_page or self.no_title or (self.no_media and self.media_counts)


def outcome_of(result: ParseResult, *, media_counts: bool = True) -> ParseOutcome:
    """Judge one detail `ParseResult`. Pure; call it only for a fetched, not-removed detail page.

    `media_counts` is False for a site whose media come from the listing card
    (`crawler.sites.base.has_card_media_only`); the counter is still recorded, it just cannot
    make the parse blind.
    """
    page = result.page
    if page is None:
        return ParseOutcome(no_page=True, media_counts=media_counts)
    return ParseOutcome(
        no_page=False,
        no_title=not page.title.strip(),
        no_media=not result.media,
        no_duration=page.duration_s is None,
        no_performers=not page.performers,
        media_counts=media_counts,
    )


@dataclass(frozen=True)
class Window:
    """Counted parses over one time window."""

    parses: int = 0
    blind: int = 0

    @property
    def share(self) -> float | None:
        return self.blind / self.parses if self.parses else None


@dataclass(frozen=True)
class Verdict:
    status: str
    detail: str

    @property
    def broken(self) -> bool:
        return self.status == AdapterHealth.Status.BROKEN


def judge(
    recent: Window, baseline: Window, *, was_broken: bool, media_counts: bool = True
) -> Verdict:
    """The whole rule, pure. `was_broken` is the previous verdict, for the hysteresis.

    `media_counts` only changes the wording: what counts as blind was already decided per parse
    (`ParseOutcome.blind`) and per hour (`_sum`). Saying "no media" to an operator whose site never
    publishes one on a video page is how the false alarm of 2026-09-19 read as a site break.
    """
    share = recent.share
    if share is None or recent.parses < MIN_PARSES:
        if was_broken:
            # Never let silence repair an adapter: a broken parser often means no pages at all.
            return Verdict(
                AdapterHealth.Status.BROKEN,
                f"still broken: only {recent.parses} parse(s) in {WINDOW_HOURS} h, too few to "
                "show recovery",
            )
        return Verdict(
            AdapterHealth.Status.UNKNOWN,
            f"{recent.parses} detail parse(s) in {WINDOW_HOURS} h, fewer than {MIN_PARSES}",
        )
    limit = RECOVERED_SHARE if was_broken else BROKEN_SHARE
    context = ""
    if (before := baseline.share) is not None:
        context = f", baseline {before:.1%} over {BASELINE_HOURS} h"
    if share >= limit:
        blind_means = "no page, no title or no media" if media_counts else "no page or no title"
        return Verdict(
            AdapterHealth.Status.BROKEN,
            f"{recent.blind} of {recent.parses} detail parses yielded {blind_means} "
            f"({share:.1%}, threshold {limit:.0%}{context})",
        )
    return Verdict(
        AdapterHealth.Status.OK,
        f"{recent.blind} of {recent.parses} detail parses blind ({share:.1%}{context})",
    )


def hour_floor(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


# --- database ---------------------------------------------------------------------------------


def record_parse(site_id: int, outcome: ParseOutcome, *, now: datetime | None = None) -> None:
    """Add one detail parse to this site's current hour bucket.

    One statement, so every per-site worker and every crawl-runtime thread can write the same
    bucket without a lock or a read-modify-write. Best effort: the counters are diagnostics, and a
    failure here must never lose the page the worker just parsed (the caller swallows).
    """
    hour = hour_floor(now or timezone.now())
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO crawler_adapterparsestat
                (site_id, hour, parses, no_page, no_title, no_media, no_duration, no_performers,
                 llm_repaired)
            VALUES (%s, %s, 1, %s, %s, %s, %s, %s, 0)
            ON CONFLICT (site_id, hour) DO UPDATE SET
                parses = crawler_adapterparsestat.parses + 1,
                no_page = crawler_adapterparsestat.no_page + EXCLUDED.no_page,
                no_title = crawler_adapterparsestat.no_title + EXCLUDED.no_title,
                no_media = crawler_adapterparsestat.no_media + EXCLUDED.no_media,
                no_duration = crawler_adapterparsestat.no_duration + EXCLUDED.no_duration,
                no_performers = crawler_adapterparsestat.no_performers + EXCLUDED.no_performers
            """,
            [
                site_id,
                hour,
                int(outcome.no_page),
                int(outcome.no_title),
                int(outcome.no_media),
                int(outcome.no_duration),
                int(outcome.no_performers),
            ],
        )


def record_repair(site_id: int, *, now: datetime | None = None) -> None:
    """Count one parse the LLM repair filled a field in (ADR 0042). Same bucket, same discipline."""
    hour = hour_floor(now or timezone.now())
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO crawler_adapterparsestat (site_id, hour, parses, llm_repaired)
            VALUES (%s, %s, 0, 1)
            ON CONFLICT (site_id, hour) DO UPDATE SET
                llm_repaired = crawler_adapterparsestat.llm_repaired + 1
            """,
            [site_id, hour],
        )


@dataclass(frozen=True)
class SiteReport:
    """One site's numbers, for the CLI and the admin. Counters only."""

    site_id: int
    domain: str
    status: str
    detail: str
    recent: Window
    baseline: Window
    no_page: int
    no_title: int
    no_media: int
    no_duration: int
    no_performers: int
    llm_repaired: int
    broken_since: datetime | None
    #: False when this site's media come from the listing card, so `no_media` is context here.
    media_counts: bool = True


def _sum(
    site: Site, start: datetime, end: datetime, *, media_counts: bool = True
) -> dict[str, int]:
    rows = AdapterParseStat.objects.filter(site=site, hour__gte=start, hour__lt=end)
    totals = {
        name: 0
        for name in (
            "parses",
            "no_page",
            "no_title",
            "no_media",
            "no_duration",
            "no_performers",
            "llm_repaired",
            "blind",
        )
    }
    for row in rows:
        totals["parses"] += row.parses
        totals["no_page"] += row.no_page
        totals["no_title"] += row.no_title
        totals["no_media"] += row.no_media
        totals["no_duration"] += row.no_duration
        totals["no_performers"] += row.no_performers
        totals["llm_repaired"] += row.llm_repaired
        # A row is an hour's sum, so the three cannot be re-separated into parses. `no_page` is
        # disjoint from the other two by construction (`outcome_of` sets nothing else on it),
        # while a parse can be both `no_title` and `no_media`. Taking the larger of those two is
        # the *lower* bound of blind parses in the hour: deliberately, because over-counting
        # would raise the share and this number is what mails the operator.
        # On a `card_media_only` site `no_media` is every parse and means nothing (module
        # docstring), so only the title half is left.
        blind = row.no_title if not media_counts else max(row.no_title, row.no_media)
        totals["blind"] += row.no_page + blind
    return totals


def media_counts_for(site: Site) -> bool:
    """Does an empty detail-page media tuple mean anything on this site?

    Asked of the registered adapter, never stored: the counters are written by whatever code was
    running at the time, and the one place that decides what they mean is here (module docstring).
    An unregistered domain answers yes — the conservative default, which alarms.
    """
    adapter = get_adapter(site.domain)
    return adapter is None or not has_card_media_only(adapter)


def evaluate(site: Site, *, now: datetime | None = None, write: bool = True) -> SiteReport:
    """Judge one site's adapter and store the verdict. Reads counters only; writes one row."""
    now = now or timezone.now()
    media_counts = media_counts_for(site)
    recent_totals = _sum(site, now - timedelta(hours=WINDOW_HOURS), now, media_counts=media_counts)
    baseline_totals = _sum(
        site,
        now - timedelta(hours=BASELINE_HOURS + WINDOW_HOURS),
        now - timedelta(hours=WINDOW_HOURS),
        media_counts=media_counts,
    )
    recent = Window(recent_totals["parses"], recent_totals["blind"])
    baseline = Window(baseline_totals["parses"], baseline_totals["blind"])
    record = AdapterHealth.objects.filter(site=site).first()
    was_broken = record is not None and record.status == AdapterHealth.Status.BROKEN
    verdict = judge(recent, baseline, was_broken=was_broken, media_counts=media_counts)
    # The clock starts at the first evaluation that found it broken and is cleared the moment it
    # is not: an operator asking "since when" wants the first sighting, not the latest pass.
    previous = record.broken_since if record is not None else None
    broken_since = (previous or now) if verdict.broken else None
    if write:
        AdapterHealth.objects.update_or_create(
            site=site,
            defaults={
                "status": verdict.status,
                "detail": verdict.detail,
                "checked_at": now,
                "broken_since": broken_since,
                "parses": recent.parses,
                "blind_share": recent.share or 0.0,
                "baseline_blind_share": baseline.share,
                "llm_repairs_today": recent_totals["llm_repaired"],
            },
        )
    return SiteReport(
        site_id=site.pk,
        domain=site.domain,
        status=verdict.status,
        detail=verdict.detail,
        recent=recent,
        baseline=baseline,
        no_page=recent_totals["no_page"],
        no_title=recent_totals["no_title"],
        no_media=recent_totals["no_media"],
        no_duration=recent_totals["no_duration"],
        no_performers=recent_totals["no_performers"],
        llm_repaired=recent_totals["llm_repaired"],
        broken_since=broken_since,
        media_counts=media_counts,
    )


def evaluate_all(
    *, domain: str | None = None, now: datetime | None = None, write: bool = True
) -> list[SiteReport]:
    sites = Site.objects.filter(enabled=True).order_by("domain")
    if domain:
        sites = Site.objects.filter(domain=domain)
    return [evaluate(site, now=now, write=write) for site in sites]


def render_text(reports: list[SiteReport]) -> str:
    """The `adapter_health` command's report. Domains and counters, never a URL or a title."""
    lines = [
        f"{'site':24} {'status':8} {'parses':>7} {'blind':>7} {'noPage':>7} {'noTitle':>8} "
        f"{'noMedia':>8} {'noDur':>7} {'noPerf':>7} {'repaired':>9}",
    ]
    for report in reports:
        share = report.recent.share
        # A `*` marks a site whose media come from the listing card: its noMedia column is every
        # parse and says nothing (`crawler.sites.base.has_card_media_only`).
        name = report.domain if report.media_counts else f"{report.domain} *"
        lines.append(
            f"{name:24} {report.status:8} {report.recent.parses:7d} "
            f"{'-' if share is None else f'{share:6.1%}':>7} {report.no_page:7d} "
            f"{report.no_title:8d} {report.no_media:8d} {report.no_duration:7d} "
            f"{report.no_performers:7d} {report.llm_repaired:9d}"
        )
    broken = [report for report in reports if report.status == AdapterHealth.Status.BROKEN]
    lines.append("")
    if broken:
        lines.append(f"{len(broken)} adapter(s) look broken — fix the parser, they are the truth:")
        lines += [f"  - {report.domain}: {report.detail}" for report in broken]
    else:
        lines.append("No adapter looks broken.")
    lines.append(
        "noDur/noPerf are context only: their day-to-day swing on a healthy adapter is larger "
        "than most breaks (crawler/adapter_health.py)."
    )
    if any(not report.media_counts for report in reports):
        lines.append(
            "* the site's video pages carry no media of their own (it travels with the task from "
            "the listing card), so noMedia is context only there too."
        )
    return "\n".join(lines)
