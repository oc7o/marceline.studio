"""Prospect research (docs/prospect-research.md): how much of a prospect's content circulates.

Before outreach to a studio or creator we have no originals from, staff want evidence: distinct
videos and photo sets of theirs on the sites we index, how many matching copies, where hosted.
This module counts that from what is already indexed. It is staff-only, writes nothing, and its
output carries page ids, site domains and counts — never media, titles or URLs.

**Attribution is by names and PDQ clusters only.** A page belongs to a prospect when their name,
an alias or a studio name (all in `Creator.aliases`) appears on it as whole words, or when it was
found on their own profile page. Never by faces or any other biometric processing.

Two strengths, because an outreach claim must be defensible:
- `strong` — the site itself attributes the page: the name is a performer credit or a tag, or the
  page was listed on the prospect's profile page (a LISTING seed of theirs and its pagination);
- `title` — the name appears only in the title (compilations, "looks like", name collisions).

The claim a report may carry is built from `strong` only; the loose total (`strong` + `title`) is
reported beside it and is never the claim. A seeded, hash-ranked sample of strong page ids is what
staff verify by hand (`index.ProspectSampleCheck`) before any number leaves the building.

**A number is refused by default** (`claim_blockers`). The gate is deterministic, not
advisory: without a stated research basis, without a fully checked sample, without a measured
precision at or above `CLAIM_MIN_PRECISION`, below `CLAIM_MIN_SITES` independent sites, or on a
name too short or too collision-prone to attribute by, `headline()` states the blockers and no
count at all. When it does pass, the sentence carries "approximately" (the pipeline both misses
pages and falsely attributes them, so the count is not bounded below), the "as of" date and the
sample precision — there is no code path that formats the number without them.

Lives in the project package because it needs `catalog`, `crawler`, `index` and `matching`.
"""

import hashlib
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.expressions import RawSQL
from django.utils import timezone

from catalog.hosting.jurisdiction import jurisdiction_of
from catalog.hosting.services import normalize_hostname
from catalog.models import Creator, HostingRecord, Site, WatchlistEntry
from catalog.suppression import is_suppressed
from crawler.models import CrawlTask
from crawler.services import match_creators, normalize_url
from crawler.sites.common import creator_names, name_keys, normalize_name, slugify
from crawler.sites.registry import get_adapter
from index.models import Media, Page, ProspectSampleCheck
from matching.models import ClusterMember

SAMPLE_SIZE = 10

# The claim gate. These are product policy, not statutory thresholds; the reasoning for each is in
# docs/prospect-research.md.
CLAIM_MIN_SITES = 2  # no multi-site statement from a single site's attribution
CLAIM_MIN_PRECISION = 0.80  # below this the sample says the number is wrong too often to use
MIN_DISTINCTIVE_CHARS = 6  # a single short token ("Anna", "Mia") attributes strangers


class Strength(StrEnum):
    STRONG = "strong"  # performer credit, tag, or listed on their profile page
    TITLE = "title"  # name in the title only


def attribution_strength(
    creator: Creator,
    *,
    title: str,
    tags: Iterable[object],
    performers: Iterable[object],
    on_profile_page: bool,
) -> Strength | None:
    """How strongly a page's own metadata ties it to `creator`; None when it does not at all.

    Whole-word, ASCII-normalised name/alias matching, the same rule the crawler links creators by
    (`crawler.services.match_creators`). Pure.
    """
    credits = [text for text in (*performers, *tags) if isinstance(text, str)]
    if on_profile_page or match_creators(credits, [creator]):
        return Strength.STRONG
    if match_creators([title], [creator]):
        return Strength.TITLE
    return None


@dataclass(frozen=True)
class AttributedPage:
    page_id: int
    site: str
    strength: Strength
    photo: bool  # a photo post (media of kind `photo`), not a video page
    cluster_id: int | None
    availability: str
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True)
class SiteHosting:
    hoster: str  # "AS<n>", or "" when no hosting record names one
    jurisdiction: str


@dataclass
class Tally:
    pages: int = 0
    pages_by_site: dict[str, int] = field(default_factory=dict)
    distinct_videos: int = 0
    photo_sets: int = 0
    copies_per_video_median: float | None = None
    copies_per_video_max: int | None = None
    sites: int = 0
    hosters: int = 0
    sites_by_jurisdiction: dict[str, int] = field(default_factory=dict)
    first_seen: str | None = None
    last_seen: str | None = None
    playable: int = 0
    removed: int = 0
    availability_unknown: int = 0
    share_online: float | None = None


def tally(
    pages: Sequence[AttributedPage],
    cluster_sizes: dict[int, int],
    hosting: dict[str, SiteHosting],
) -> Tally:
    """Counts over one set of attributed pages. Pure.

    A distinct video is a cluster with at least one of these pages, or one of these pages in no
    cluster; a cluster with any video page counts as a video, otherwise as a photo set. Copies per
    distinct video are the cluster's members (every copy, attributed or not), 1 when unclustered.
    """
    result = Tally(pages=len(pages))
    if not pages:
        return result
    result.pages_by_site = dict(sorted(Counter(page.site for page in pages).items()))
    video_clusters = {p.cluster_id for p in pages if not p.photo and p.cluster_id is not None}
    photo_clusters = {
        p.cluster_id for p in pages if p.photo and p.cluster_id is not None
    } - video_clusters
    loose_videos = sum(1 for p in pages if not p.photo and p.cluster_id is None)
    loose_photos = sum(1 for p in pages if p.photo and p.cluster_id is None)
    result.distinct_videos = len(video_clusters) + loose_videos
    result.photo_sets = len(photo_clusters) + loose_photos
    copies = [max(cluster_sizes.get(cid, 1), 1) for cid in sorted(video_clusters)]
    copies += [1] * loose_videos
    if copies:
        result.copies_per_video_median = float(statistics.median(copies))
        result.copies_per_video_max = max(copies)
    sites = sorted(result.pages_by_site)
    result.sites = len(sites)
    unknown = SiteHosting("", "unknown")
    result.hosters = len({hosting.get(s, unknown).hoster for s in sites} - {""})
    result.sites_by_jurisdiction = dict(
        sorted(Counter(hosting.get(s, unknown).jurisdiction for s in sites).items())
    )
    result.first_seen = min(p.first_seen for p in pages).date().isoformat()
    result.last_seen = max(p.last_seen for p in pages).date().isoformat()
    states = Counter(p.availability for p in pages)
    result.playable = states[Page.Availability.PLAYABLE]
    result.removed = states[Page.Availability.REMOVED]
    result.availability_unknown = len(pages) - result.playable - result.removed
    result.share_online = round(result.playable / len(pages), 3)
    return result


def sample_page_ids(page_ids: Iterable[int], *, seed: int, size: int = SAMPLE_SIZE) -> list[int]:
    """A reproducible random sample: the `size` ids ranking first by sha256(seed, id). Pure.

    Ranking by a keyed hash rather than shuffling keeps the sample stable while pages are added:
    a new page only enters when it outranks a sampled one, so recorded verdicts stay useful.
    """
    ranked = sorted(
        set(page_ids),
        key=lambda pk: hashlib.sha256(f"{seed}:{pk}".encode()).hexdigest(),
    )
    return sorted(ranked[:size])


@dataclass
class SampleCheck:
    seed: int
    page_ids: list[int]
    correct: int = 0
    wrong: int = 0
    unsure: int = 0
    unchecked: list[int] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        decided = self.correct + self.wrong
        return round(self.correct / decided, 3) if decided else None


class ResearchBasis(StrEnum):
    """Why we may hold research about this prospect at all, asserted by the operator per run.

    A per-person record of where an identifiable person's content appears is personal data about
    a non-customer, so research needs a stated basis. The two bases below are the only ones here,
    and neither is the default.

    Nothing stores this: it is an assertion the operator makes and the briefing records, not a new
    durable field about a non-customer.
    """

    NOT_STATED = "not_stated"
    LEGAL_PERSON = "legal_person"  # a studio/company, researched by brand and title
    EXPLICIT_OPT_IN = "explicit_opt_in"  # the person asked us to run the check, scope recorded


class Blocker(StrEnum):
    """Why no number may be stated. Codes, so a report never argues with itself in prose."""

    RESEARCH_BASIS_NOT_STATED = "research_basis_not_stated"
    NO_STRONG_ATTRIBUTION = "no_strong_attribution"
    SAMPLE_UNCHECKED = "sample_unchecked"
    PRECISION_UNKNOWN = "precision_unknown"
    PRECISION_BELOW_FLOOR = "precision_below_floor"
    ONE_SITE_ONLY = "one_site_only"
    NAME_NOT_DISTINCTIVE = "name_not_distinctive"


def ambiguous_names(names: Iterable[str], other_creator_names: Iterable[str]) -> list[str]:
    """Names too weak to attribute a page by: a short single token, or one we know twice. Pure.

    Two people sharing a stage name are indistinguishable by name, and a single short token
    ("Anna") matches strangers on every site. A common-name collision is a stop
    regardless of how many pages were found: ten wrong pages do not become reliable because there
    are ten of them.
    """
    known = set(name_keys(other_creator_names))
    flagged = set()
    for name in names:
        key = normalize_name(name)
        if not key:
            continue
        if key in known or (" " not in key and len(key) < MIN_DISTINCTIVE_CHARS):
            flagged.add(name)
    return sorted(flagged)


@dataclass(frozen=True)
class Claim:
    """The only thing an approach may be built on — or the reasons there is nothing. Pure data."""

    as_of: str
    basis: ResearchBasis
    distinct_videos: int
    photo_sets: int
    sites: int
    sample_size: int
    sample_decided: int
    sample_correct: int
    precision: float | None
    ambiguous_names: list[str]
    blockers: list[Blocker]

    @property
    def usable(self) -> bool:
        return not self.blockers


def claim_blockers(
    strong: Tally,
    sample: SampleCheck,
    *,
    basis: ResearchBasis,
    ambiguous: Sequence[str],
) -> list[Blocker]:
    """Every reason this prospect's counts may not be stated to anyone. Pure, deterministic."""
    blockers: list[Blocker] = []
    if basis is ResearchBasis.NOT_STATED:
        blockers.append(Blocker.RESEARCH_BASIS_NOT_STATED)
    if not strong.distinct_videos and not strong.photo_sets:
        blockers.append(Blocker.NO_STRONG_ATTRIBUTION)
    if ambiguous:
        blockers.append(Blocker.NAME_NOT_DISTINCTIVE)
    if strong.sites < CLAIM_MIN_SITES:
        blockers.append(Blocker.ONE_SITE_ONLY)
    if sample.unchecked:
        blockers.append(Blocker.SAMPLE_UNCHECKED)
    if sample.precision is None:
        blockers.append(Blocker.PRECISION_UNKNOWN)
    elif sample.precision < CLAIM_MIN_PRECISION:
        blockers.append(Blocker.PRECISION_BELOW_FLOOR)
    return blockers


def build_claim(
    strong: Tally,
    sample: SampleCheck,
    *,
    as_of: date,
    basis: ResearchBasis,
    ambiguous: Sequence[str],
) -> Claim:
    """The claim and its gate for one prospect. Pure."""
    return Claim(
        as_of=as_of.isoformat(),
        basis=basis,
        distinct_videos=strong.distinct_videos,
        photo_sets=strong.photo_sets,
        sites=strong.sites,
        sample_size=len(sample.page_ids),
        sample_decided=sample.correct + sample.wrong,
        sample_correct=sample.correct,
        precision=sample.precision,
        ambiguous_names=sorted(ambiguous),
        blockers=claim_blockers(strong, sample, basis=basis, ambiguous=ambiguous),
    )


def headline(claim: Claim) -> str:
    """The one sentence an approach may be built on, or the refusal. Pure.

    There is no branch that formats the count without the "as of" date and the measured precision:
    a missing precision is itself a blocker, so a number and its precision are the same sentence.
    """
    if not claim.usable:
        return "no defensible number — " + ", ".join(str(b) for b in claim.blockers)
    parts = [f"approximately {claim.distinct_videos} distinct video(s)"]
    if claim.photo_sets:
        parts.append(f"{claim.photo_sets} photo set(s)")
    assert claim.precision is not None  # PRECISION_UNKNOWN would have blocked
    return (
        " and ".join(parts)
        + f" with matching copies on {claim.sites} site(s), as of {claim.as_of} "
        + f"(sample precision {claim.precision:.0%} on {claim.sample_decided} of "
        + f"{claim.sample_size} sampled pages checked by hand; not verified as theirs and "
        + "not verified as unauthorised)"
    )


@dataclass
class ProspectReport:
    creator: str
    purpose: str
    crawl: bool
    claim: Claim
    headline: str
    strong: Tally
    loose: Tally
    title_only_by_site: dict[str, int]
    flagged_pages_excluded: int
    sample: SampleCheck

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["sample"]["precision"] = self.sample.precision
        data["claim"]["usable"] = self.claim.usable
        return data


def profile_page_urls(creator: Creator) -> list[str]:
    """The creator's profile start URLs (LISTING seeds) on every site with an adapter."""
    urls: set[str] = set()
    for site in Site.objects.order_by("pk"):
        adapter = get_adapter(site.domain)
        if adapter is None:
            continue
        for spec in adapter.seed_tasks(site, [creator]):
            if spec.kind == CrawlTask.Kind.LISTING:
                urls.add(normalize_url(spec.url))
    return sorted(urls)


def _name_patterns(creator: Creator) -> list[str]:
    slugs = {slugify(name) for name in creator_names(creator)} - {""}
    variants = {v for slug in slugs for v in (slug, slug.replace("-", ""), slug.replace("-", "_"))}
    return sorted(f"%{variant}%" for variant in variants)


def _profile_page_sql() -> str:
    """Pages listed on the profile pages at the first parameter's URLs, through pagination whose URL
    still names the creator (second parameter, LIKE patterns) — so a "related profiles" link on a
    profile page never makes someone else's catalogue theirs."""
    task = CrawlTask._meta.db_table
    page = Page._meta.db_table
    detail = CrawlTask.Kind.DETAIL.value
    return f"""
        WITH RECURSIVE reach(id, kind) AS (
            SELECT id, kind FROM {task} WHERE url = ANY(%s)
            UNION
            SELECT child.id, child.kind FROM {task} child
            JOIN reach ON child.discovered_from_id = reach.id
            WHERE reach.kind <> '{detail}'
              AND (child.kind = '{detail}' OR lower(child.url) LIKE ANY(%s))
        )
        SELECT p.id FROM {page} p JOIN {task} t ON t.url = p.url
        JOIN reach ON reach.id = t.id WHERE reach.kind = '{detail}'
    """


def attributed_pages(creator: Creator) -> tuple[list[AttributedPage], int]:
    """Every safety-clear page attributed to `creator`, and how many flagged pages were left out.

    Candidates are pages the crawler linked to the creator and pages listed on their profile pages;
    `attribution_strength` then grades each from its stored metadata.
    """
    urls = profile_page_urls(creator)
    patterns = _name_patterns(creator)
    on_profile_page = (
        Q(pk__in=RawSQL(_profile_page_sql(), [urls, patterns]))
        if urls and patterns
        else Q(pk__in=[])
    )
    candidates = Page.objects.filter(Q(creators=creator) | on_profile_page).distinct()
    flagged = candidates.exclude(safety_status=Page.SafetyStatus.CLEAR).count()
    profile_ids = set(Page.objects.filter(on_profile_page).values_list("pk", flat=True))
    membership = ClusterMember.objects.filter(page_id=OuterRef("pk")).exclude(
        status=ClusterMember.Status.REJECTED
    )
    rows = (
        candidates.filter(safety_status=Page.SafetyStatus.CLEAR)
        .annotate(
            is_photo=Exists(Media.objects.filter(page_id=OuterRef("pk"), kind=Media.Kind.PHOTO)),
            cluster=Subquery(membership.values("cluster_id")[:1]),
        )
        .values_list(
            "pk",
            "site__domain",
            "title",
            "tags",
            "raw_extraction",
            "is_photo",
            "cluster",
            "availability",
            "first_seen",
            "last_seen",
        )
        .order_by("pk")
    )
    pages: list[AttributedPage] = []
    for pk, site, title, tags, extraction, photo, cluster, availability, first, last in rows:
        performers = extraction.get("performers", []) if isinstance(extraction, dict) else []
        strength = attribution_strength(
            creator,
            title=title,
            tags=tags if isinstance(tags, list) else [],
            performers=performers if isinstance(performers, list) else [],
            on_profile_page=pk in profile_ids,
        )
        if strength is not None:
            pages.append(
                AttributedPage(pk, site, strength, photo, cluster, availability, first, last)
            )
    return pages, flagged


def cluster_sizes(cluster_ids: Iterable[int]) -> dict[int, int]:
    """Safety-clear, non-rejected members per cluster: the copies of one video."""
    rows = (
        ClusterMember.objects.filter(cluster_id__in=set(cluster_ids))
        .exclude(status=ClusterMember.Status.REJECTED)
        .filter(page__safety_status=Page.SafetyStatus.CLEAR)
        .values_list("cluster_id", flat=True)
    )
    return dict(Counter(rows))


def site_hosting(domains: Iterable[str]) -> dict[str, SiteHosting]:
    """Hoster (origin AS) and jurisdiction of each site's own hostname (ADR 0027 records)."""
    by_host = {normalize_hostname(domain): domain for domain in domains}
    result = {domain: SiteHosting("", "unknown") for domain in by_host.values()}
    for record in HostingRecord.objects.filter(hostname__in=list(by_host)):
        result[by_host[record.hostname]] = SiteHosting(
            f"AS{record.asn}" if record.asn else "", jurisdiction_of(record.country)
        )
    return result


def check_sample(creator: Creator, strong_ids: Iterable[int], *, seed: int) -> SampleCheck:
    sample = SampleCheck(seed, sample_page_ids(strong_ids, seed=seed))
    verdicts = dict(
        ProspectSampleCheck.objects.filter(
            creator=creator, page_id__in=sample.page_ids
        ).values_list("page_id", "verdict")
    )
    for page_id in sample.page_ids:
        verdict = verdicts.get(page_id)
        if verdict == ProspectSampleCheck.Verdict.CORRECT:
            sample.correct += 1
        elif verdict == ProspectSampleCheck.Verdict.WRONG:
            sample.wrong += 1
        elif verdict == ProspectSampleCheck.Verdict.UNSURE:
            sample.unsure += 1
        else:
            sample.unchecked.append(page_id)
    return sample


def other_creator_names(creator: Creator) -> list[str]:
    """Every name and alias of every other creator we may use, for the collision check.

    `usable()` on purpose: a suppressed name must not come back even as the reason another
    prospect's name was called ambiguous (docs/suppression.md).
    """
    names: list[str] = []
    for other in Creator.objects.usable().exclude(pk=creator.pk).only("name", "aliases"):
        names += creator_names(other)
    return names


def prospect_report(
    creator: Creator,
    *,
    seed: int = 0,
    basis: ResearchBasis = ResearchBasis.NOT_STATED,
    now: datetime | None = None,
) -> ProspectReport:
    """Everything a report says about one prospect. Read-only.

    A suppressed name (docs/suppression.md) has no report at all: research is exactly the
    processing such a person objected to, and a report is how a name reaches an outreach list.
    """
    if is_suppressed(creator):
        raise ValueError(
            f"{creator.name!r} is suppressed: no prospect report, no outreach (docs/suppression.md)"
        )
    pages, flagged = attributed_pages(creator)
    strong = [page for page in pages if page.strength == Strength.STRONG]
    sizes = cluster_sizes(page.cluster_id for page in pages if page.cluster_id is not None)
    hosting = site_hosting({page.site for page in pages})
    strong_tally = tally(strong, sizes, hosting)
    sample = check_sample(creator, (page.page_id for page in strong), seed=seed)
    claim = build_claim(
        strong_tally,
        sample,
        as_of=(now or timezone.now()).date(),
        basis=basis,
        ambiguous=ambiguous_names(creator_names(creator), other_creator_names(creator)),
    )
    entry = WatchlistEntry.objects.filter(creator=creator).first()
    return ProspectReport(
        creator=creator.name,
        purpose=entry.purpose if entry else "",
        crawl=bool(entry and entry.crawl),
        claim=claim,
        headline=headline(claim),
        strong=strong_tally,
        loose=tally(pages, sizes, hosting),
        title_only_by_site=dict(
            sorted(Counter(p.site for p in pages if p.strength == Strength.TITLE).items())
        ),
        flagged_pages_excluded=flagged,
        sample=sample,
    )


def render_text(report: ProspectReport) -> list[str]:
    """Plain-text lines for staff: page ids, site domains and counts only. Pure."""
    watch = f"watchlist {report.purpose}, crawl {'on' if report.crawl else 'off'}"
    lines = [
        f"Prospect: {report.creator} ({watch if report.purpose else 'not on the watchlist'})",
        f"  As of: {report.claim.as_of}; research basis stated: {report.claim.basis}",
        f"  Claim: {report.headline}",
    ]
    if report.claim.ambiguous_names:
        lines.append("  Names too weak to attribute by: " + ", ".join(report.claim.ambiguous_names))
    for label, part in (("strong", report.strong), ("loose (strong + title only)", report.loose)):
        copies = (
            f"median {part.copies_per_video_median:g}, max {part.copies_per_video_max}"
            if part.copies_per_video_max is not None
            else "n/a"
        )
        online = f"{part.share_online:.0%}" if part.share_online is not None else "n/a"
        jurisdictions = ", ".join(f"{k} {v}" for k, v in part.sites_by_jurisdiction.items())
        lines += [
            f"  {label}: {part.pages} page(s), {part.distinct_videos} distinct video(s), "
            f"{part.photo_sets} photo set(s), matching copies per video {copies}",
            f"    {part.sites} site(s), {part.hosters} hoster(s), jurisdictions: "
            f"{jurisdictions or 'n/a'}; seen {part.first_seen or 'n/a'} .. "
            f"{part.last_seen or 'n/a'}; online {online} ({part.playable} playable, "
            f"{part.removed} removed, {part.availability_unknown} unknown)",
        ]
    lines.append("  Pages per site (strong / title only):")
    for site in sorted(report.loose.pages_by_site):
        lines.append(
            f"    {site}: {report.strong.pages_by_site.get(site, 0)} / "
            f"{report.title_only_by_site.get(site, 0)}"
        )
    if report.flagged_pages_excluded:
        lines.append(f"  Safety-flagged pages excluded: {report.flagged_pages_excluded}")
    sample = report.sample
    precision = f"{sample.precision:.0%}" if sample.precision is not None else "n/a"
    lines += [
        f"  Verification sample (seed {sample.seed}, strong pages, open in the moderation UI): "
        + (", ".join(str(pk) for pk in sample.page_ids) or "none"),
        f"    verdicts: {sample.correct} correct, {sample.wrong} wrong, {sample.unsure} unsure, "
        f"{len(sample.unchecked)} unchecked; precision {precision}",
    ]
    if sample.unchecked:
        lines.append("    unchecked: " + ", ".join(str(pk) for pk in sample.unchecked))
    return lines


# What the operator must change before a blocked claim could be stated. One line per code, so the
# briefing never leaves a refusal without a next step.
REMEDIES: dict[Blocker, str] = {
    Blocker.RESEARCH_BASIS_NOT_STATED: (
        "state the basis with --basis: legal_person (a company, researched by brand and title) "
        "or explicit_opt_in (the person asked for this check, scope and date recorded). For a "
        "natural person without an opt-in there is no basis"
    ),
    Blocker.NO_STRONG_ATTRIBUTION: (
        "nothing on the indexed sites is attributed by the site itself (performer credit, tag or "
        "profile listing). A title-only total is never a claim"
    ),
    Blocker.NAME_NOT_DISTINCTIVE: (
        "the name or an alias is a short single token or one we already know for someone else; "
        "a collision is a stop at any page count. Narrow the aliases or drop the prospect"
    ),
    Blocker.ONE_SITE_ONLY: (
        f"attribution rests on fewer than {CLAIM_MIN_SITES} independent sites; no statement about "
        "circulation may be built on one site"
    ),
    Blocker.SAMPLE_UNCHECKED: (
        "record a verdict for every sampled page id in the admin (Index -> Prospect sample "
        "checks), then re-run with the same seed"
    ),
    Blocker.PRECISION_UNKNOWN: (
        "no sampled page has a correct/wrong verdict yet, so nothing is measured"
    ),
    Blocker.PRECISION_BELOW_FLOOR: (
        f"measured precision is below {CLAIM_MIN_PRECISION:.0%}: the sample says the attribution "
        "is wrong too often to state. Fix the aliases or wait for more clustering; do not state "
        "the number with a caveat"
    ),
}

# Never in an approach, whatever the gate says.
NEVER_IN_AN_APPROACH = (
    "a URL, a hoster link, a site name or a page title",
    "a thumbnail, screenshot, frame or any image, and any statement that we watched the content",
    "a page id, a sha256, a PDQ distance, a cluster id or any other internal identifier",
    'the words "leak", "stolen", "pirated", "infringing", "illegal" or "verified"',
    "a claim that the copies are unauthorised, that rights are held, or that they can be removed",
    "an individual legal assessment of their case (that is a lawyer's, not ours)",
    "urgency of any kind: deadlines, countdowns, 'more found today', reminders, follow-ups",
    "anything sensitive in a subject line, on an envelope, or in a calendar or CRM preview",
    "a number without the 'as of' date and the sample precision in the same sentence",
    'the word "at least": the pipeline both misses pages and attributes wrongly',
)

# Channels. Electronic channels need the recipient's prior express consent, businesses included.
LAWFUL_CHANNELS = (
    "inbound (they contact us)",
    "a referral or an introduction by someone they already deal with",
    "an addressed postal letter to a business address, plain envelope, no 'confidential' marking",
    "in person at a trade event (note: a public fair is not a confidential setting)",
    "an agency or trade association as intermediary",
)
UNLAWFUL_CHANNELS = (
    "email, including to an address published in an Impressum",
    "a web contact form",
    "a social-platform or messenger DM, and LinkedIn messages or connection pitches",
    "a cold telephone call, including to a studio's published business number",
    'a "may we send you information?" message through any of the above',
)


def render_briefing(report: ProspectReport) -> list[str]:
    """The operator-facing outreach briefing for one prospect. Pure.

    Deliberately narrower than `render_text`: **no page ids, no site domains, no hosters, no
    dates other than the "as of" day**. It is the page an operator reads before deciding whether
    to approach someone, so it holds only what could survive being read by that person — a
    factual register: pages naming them, candidate pages, matching copies,
    content groups; never a leak, never an infringement, never a verified anything.
    """
    claim = report.claim
    lines = [
        f"OUTREACH BRIEFING — {report.creator}",
        f"  As of:            {claim.as_of}  (every number below is that day's state, no other)",
        f"  Research basis:   {claim.basis}",
        "",
        "1. Decision",
    ]
    if claim.usable:
        lines += [
            "   An approach may be prepared. The only statement it may carry is:",
            "",
            f"     As of {claim.as_of}, on the sites we monitor we identified approximately "
            f"{claim.distinct_videos} distinct video(s)"
            + (f" and {claim.photo_sets} photo set(s)" if claim.photo_sets else "")
            + f" whose pages name {report.creator}, across {claim.sites} sites. We reviewed the "
            f"stored text of {claim.sample_decided} of those pages by hand; "
            f"{claim.sample_correct} appeared consistent with the public identity we searched "
            "for. These are candidate pages, not verified findings: we have not confirmed that "
            "the content is theirs, nor whether its publication was authorised.",
            "",
            "   Say it as separate facts if asked — pages naming them, sites, estimated content",
            "   groups — never as one number called anything else, and never without the date.",
        ]
    else:
        lines += [
            "   NO APPROACH. No number, count or characterisation of this prospect's situation",
            "   may leave the building. What would have to change:",
        ]
        lines += [f"     - {blocker}: {REMEDIES[blocker]}" for blocker in claim.blockers]
    lines += [
        "",
        "2. What the research measured (internal only — not for an approach)",
        f"   Site-attributed content groups: {claim.distinct_videos} video(s), "
        f"{claim.photo_sets} photo set(s), across {claim.sites} site(s).",
        f"   Hand-checked sample: {claim.sample_decided} of {claim.sample_size} pages decided, "
        + (
            f"precision {claim.precision:.0%}."
            if claim.precision is not None
            else "precision not measured."
        ),
        "   Attribution is by name, alias and PDQ cluster only. No biometrics of any kind, and",
        "   no face, body or person matching — not even as a hint.",
    ]
    if claim.ambiguous_names:
        lines.append("   Names too weak to attribute by: " + ", ".join(claim.ambiguous_names) + ".")
    if report.flagged_pages_excluded:
        lines.append(
            f"   {report.flagged_pages_excluded} safety-flagged page(s) are excluded from every "
            "count above and may never be mentioned, counted or released."
        )
    lines += [
        "",
        "3. What must never be in an approach, whatever section 1 says",
    ]
    lines += [f"   - {item}" for item in NEVER_IN_AN_APPROACH]
    lines += [
        "",
        "4. Channel",
        "   May be used:",
    ]
    lines += [f"     - {channel}" for channel in LAWFUL_CHANNELS]
    lines.append("   May not be used without their prior express consent (§ 7(2) UWG):")
    lines += [f"     - {channel}" for channel in UNLAWFUL_CHANNELS]
    lines += [
        "",
        "5. Owed at the first contact, not later",
        "   - The Art. 14 GDPR information: who the controller is, the purpose and legal basis,",
        "     what is held, where it came from, how long it is kept, and their rights. A link",
        "     only works if the notice actually exists.",
        "   - The Art. 21 objection right, brought to their attention in that first message.",
        "   - Our identity: the founder's own name plus the project name. Never an entity, an",
        "     address or a website that does not exist.",
        "   - A 'no' is an objection: stop, and keep only a do-not-contact record, never the",
        "     research.",
        "",
        "6. Standing",
        "   This briefing is internal research, not legal advice.",
        "   A suppressed name has no briefing at all (docs/suppression.md).",
    ]
    return lines
