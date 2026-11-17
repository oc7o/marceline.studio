"""Discovery evidence workflows; never enqueue crawls or enable sites."""

from collections.abc import Sequence
from dataclasses import dataclass

from django.db import transaction

from catalog.models import Creator, Site
from catalog.suppression import active_suppressions, suppressed_names
from crawler import safety, text_gate
from crawler.discovery.domains import classify, ignore_reason, ignored, normalize_domain
from crawler.discovery.kinds import ProbeFacts, Verdict, classify_probe
from crawler.discovery.telegram import parse_telegram_link
from crawler.models import (
    CandidateCreator,
    CandidatePage,
    SourceCandidate,
    TelegramChannelCandidate,
    TelegramChannelPage,
)
from crawler.services import load_safety_terms
from crawler.sites.common import creator_names, name_keys, names_person
from index.models import Page

QUERY_TEMPLATES = ("{name} leaked", "{name} fanhub leak", "{name} fanhub free videos")
# Creator-source templates (issue #118). The three
# templates above look for a *video* of a studio original on a video site; an independent creator's
# paid posts are republished as sets of stills and short clips, in places organised around the
# creator's name, around a thread, or around a link to a file host. These ask for that vocabulary
# instead — the generic words those places use about themselves, never a site's name and never a
# word that points at minors or at non-consent (`unsafe_query_reason` refuses the whole run over
# one, and a test checks every template below against the live term list).
#
# They run **only for target creators** (`marceline.targets`: rights holders and co-performers of
# owned originals, and watchlist entries with `crawl` ticked) — customers who asked us to look.
# Never for a prospect: research before opt-in must not spend a search on somebody's name in a
# leak vocabulary (docs/prospect-research.md). `plan_queries` refuses a targets-only market that
# is not given the caller's verified list of target names.
CREATOR_SOURCE_TEMPLATES = (
    "{name} leaks",
    "{name} leaked photos",
    "{name} leaked pack",
    "{name} folder download",
    "{name} leak forum",
    "{name} content free download",
)
CREATOR_SOURCE_TEMPLATES_DE = (
    "{name} leaks kostenlos",
    "{name} bilder geleakt",
    "{name} pack download",
    "{name} ordner download",
    "{name} forum geleakt",
    "{name} komplettes set",
)
# German market (issue #24): per creator or stage name, and a
# few generic queries that only look for domains (no creator linking).
GERMAN_QUERY_TEMPLATES = (
    "{name} geleakt",
    "{name} leak deutsch",
    "{name} fanhub leak deutsch",
    "{name} kostenlos video",
    "{name} ganzes video",
    "{name} clipmarket kostenlos",
    "{name} clipmarket video",
    "{name} loyalclub leak",
    "{name} fancentral leak",
    "{name} telegram",
    "{name} download",
)
GERMAN_GENERIC_QUERIES = (
    "deutsche fanhub leaks",
    "clipmarket leaks",
    "loyalclub leaks",
    "deutsche creator leaks",
    "fancentral videos kostenlos",
)
EVIDENCE_LIMIT = 20
IGNORED_NOTE = "ignored non-source domain"
INVITE_NOTE = "private invite link: off-limits, never followed; only a hash of the code is stored"
# The refusal itself moved to `crawler/text_gate.py` on 2026-09-18 (ADR 0042 amendment): the
# adapter repair has to apply the same one before it sends a page's text to an LLM provider, and
# two copies of a control is one copy too many. These names stay as they were.
FORBIDDEN_QUERY_WORDS = text_gate.FORBIDDEN_WORDS
UNSAFE_QUERY_SIGNALS = text_gate.UNSAFE_SIGNALS


@dataclass(frozen=True)
class Market:
    """Where and in which language discovery searches, and with which templates (ADR 0013).

    `location_code` / `language` None use `DISCOVERY_LOCATION_CODE` / `DISCOVERY_LANGUAGE`.

    `targets_only` marks a template set that may only ever be planned for creators the caller has
    proved to be targets (issue #118). `plan_queries` refuses it without that list, so the gate
    cannot be forgotten by a caller — and `crawler` may not import `marceline.targets`, which is
    why the list is passed in rather than looked up here.
    """

    code: str
    location_code: int | None
    language: str | None
    templates: tuple[str, ...]
    generic_queries: tuple[str, ...] = ()
    targets_only: bool = False


DEFAULT_MARKET = Market("default", None, None, QUERY_TEMPLATES)
# DataForSEO location 2276 = Germany.
GERMAN_MARKET = Market("de", 2276, "de", GERMAN_QUERY_TEMPLATES, GERMAN_GENERIC_QUERIES)
CREATOR_MARKET = Market("creator", None, None, CREATOR_SOURCE_TEMPLATES, targets_only=True)
GERMAN_CREATOR_MARKET = Market(
    "creator-de", 2276, "de", CREATOR_SOURCE_TEMPLATES_DE, targets_only=True
)
MARKETS = {
    market.code: market
    for market in (DEFAULT_MARKET, GERMAN_MARKET, CREATOR_MARKET, GERMAN_CREATOR_MARKET)
}


@dataclass(frozen=True)
class Query:
    text: str
    creator_id: int | None = None


def _suppressed_named_in(queries: list[str]) -> list[str]:
    """Suppressed names any of these free-text queries names (docs/suppression.md)."""
    if not queries:
        return []
    named = {
        suppression.creator.name
        for suppression in active_suppressions()
        if any(
            names_person(query, name_keys(creator_names(suppression.creator))) for query in queries
        )
    }
    return sorted(named)


def plan_queries(
    names: list[str],
    queries: list[str],
    *,
    market: Market = DEFAULT_MARKET,
    generic: bool = False,
    target_names: frozenset[str] | None = None,
) -> list[Query]:
    """Creator templates, then free-text queries, then the market's generic queries.

    Without names, every creator is planned unless free text or generic queries were asked for.

    `target_names` is the caller's verified list of target creators (issue #118). A
    `targets_only` market refuses to plan anything without it, and with it no creator outside the
    list is ever planned — a `--creator` naming someone who is not a target simply yields nothing.
    """
    if generic and not market.generic_queries:
        raise ValueError(f"Market {market.code} has no generic queries")
    if market.targets_only and target_names is None:
        raise ValueError(
            f"Market {market.code} runs only for target creators: plan it through "
            "`manage.py discover_creator_sources` (docs/research/creator-leak-sources.md)"
        )
    # `usable()`: unreviewed names from customer uploads never spend search budget
    # (Creator.needs_review), and a suppressed name is never sent to a search engine at all —
    # that request would be the processing the person objected to (docs/suppression.md).
    creators = Creator.objects.usable()
    if names:
        creators = creators.filter(name__in=names)
        found = set(creators.values_list("name", flat=True))
        if missing := set(names) - found:
            if suppressed := suppressed_names(missing):
                raise ValueError(
                    "Suppressed, no query may name them (docs/suppression.md): "
                    + ", ".join(suppressed)
                )
            raise ValueError("Unknown creators: " + ", ".join(sorted(missing)))
    if target_names is not None:
        # The gate, applied after the name checks so an unknown or suppressed name still gets its
        # own message: nobody outside the caller's verified target list is ever planned.
        creators = creators.filter(name__in=sorted(target_names))
    elif queries or generic:
        creators = Creator.objects.none()
    if free_text_hits := _suppressed_named_in(queries):
        # Free text bypasses the creator list, so it gets the same rule (docs/suppression.md).
        raise ValueError(
            "Query names a suppressed person (docs/suppression.md): " + ", ".join(free_text_hits)
        )
    result: list[Query] = []
    for creator in creators:
        for name in dict.fromkeys([creator.name, *creator.aliases]):
            for template in market.templates:
                result.append(Query(template.format(name=name), creator.pk))
    result.extend(Query(text) for text in dict.fromkeys(queries))
    if generic:
        result.extend(Query(text) for text in market.generic_queries)
    return list(dict.fromkeys(result))


def unsafe_query_reason(text: str, terms: Sequence[safety.CompiledTerm]) -> str:
    """Why a search query must not be sent ("" if it may): safety terms, minor near misses, or
    forbidden non-consent wording.

    `text_gate.judge` is the shared refusal (ADR 0042 amendment); this wrapper only decides how it
    is phrased. A query is **our own** text, so naming the matched word here is allowed — the
    repair path, whose text is a page's, records rule names alone.
    """
    verdict = text_gate.judge(text, terms)
    if verdict.word:
        return f"forbidden word: {verdict.word}"
    return f"safety signal: {', '.join(verdict.rules)}" if verdict.rules else ""


def unsafe_queries(plan: Sequence[Query]) -> list[tuple[Query, str]]:
    """Planned queries that must not be sent, with the reason, checked against today's terms."""
    terms = load_safety_terms()
    reasons = ((query, unsafe_query_reason(query.text, terms)) for query in plan)
    return [(query, reason) for query, reason in reasons if reason]


def excluded_domains() -> set[str]:
    return {normalize_domain(domain) for domain in Site.objects.values_list("domain", flat=True)}


def existing_site_domain(domain: str) -> str:
    """Exact match: `domain` already exists as a `Site` — this candidate *is* that site (went
    proposed -> approved -> built already, just never closed out), never a mirror of it.

    `Site.enabled` never matters here, only that the row exists: a disabled site on hold
    (e.g. site12.example) is still onboarded, not still-undecided.
    """
    return domain if domain in excluded_domains() else ""


def mirror_domain(domain: str, *, by_name: bool = True) -> str:
    """Match normalized Site domains, optionally including the same label across TLDs.

    Unchanged and still used for a *different* domain than the candidate's own — a redirect
    target (`probe.py`, `by_name=False`), or a cross-TLD label match. Callers checking the
    candidate's own domain must call `existing_site_domain` first (onboarded, not a mirror of
    itself); this function does not know which domain is "self" and an exact match here is
    correct mirror behaviour for anything but that one case.
    """
    for existing in sorted(excluded_domains()):
        if existing == domain or (by_name and existing.split(".")[0] == domain.split(".")[0]):
            return existing
    return ""


def add_note(candidate: SourceCandidate, note: str) -> None:
    if note not in candidate.notes:
        candidate.notes = "\n".join(filter(None, (candidate.notes, note)))


def approval_note(candidate: SourceCandidate) -> str:
    return f"Approved discovery candidate {candidate.pk}"


def approval_site(candidate: SourceCandidate) -> Site | None:
    """The Site this candidate's approval created; None if it existed before or was edited."""
    return Site.objects.filter(domain=candidate.domain, notes=approval_note(candidate)).first()


def undo_approval_blocker(candidate: SourceCandidate) -> str:
    """Why an approval can't be undone without losing data ("" if it can)."""
    site = approval_site(candidate)
    if site is None:
        return ""
    if site.enabled:
        return "site enabled"
    for relation in Site._meta.related_objects:
        if relation.related_model._default_manager.filter(**{relation.field.name: site}).exists():
            return "site has crawl data"
    return ""


def undo_approval(candidate: SourceCandidate) -> int | None:
    """Delete the untouched, disabled Site the approval created; returns its former pk.

    Sites that existed before the approval are kept. Callers check `undo_approval_blocker` first."""
    if blocker := undo_approval_blocker(candidate):
        raise ValueError(f"approval of {candidate.domain} can't be undone: {blocker}")
    site = approval_site(candidate)
    if site is None:
        return None
    pk = site.pk
    site.delete()
    return pk


def mark_mirror(candidate: SourceCandidate, domain: str) -> None:
    candidate.status = "ignored"
    candidate.evidence["mirror_of"] = domain
    add_note(candidate, f"mirror of {domain}")


def mark_onboarded(candidate: SourceCandidate) -> None:
    """The candidate's own domain already exists as a `Site`: proposed -> approved -> built
    already, this row just never got closed out. Never a mirror of anything."""
    candidate.status = "onboarded"
    add_note(candidate, f"already onboarded as {candidate.domain}")


def probe_facts(candidate: SourceCandidate) -> ProbeFacts:
    """Exactly the recorded fields the coverage classifier may read (issue #118)."""
    return ProbeFacts(
        domain_kind=candidate.kind,
        robots_allows=candidate.robots_allows,
        blocked_reason=candidate.blocked_reason,
        cms_guess=candidate.cms_guess,
        has_search=candidate.has_search,
        video_links_seen=candidate.video_links_seen,
        profile_links_seen=candidate.profile_links_seen,
        forum_links_seen=candidate.forum_links_seen,
        gallery_images_seen=candidate.gallery_images_seen,
        probed=candidate.probed_at is not None,
    )


def record_coverage(candidate: SourceCandidate) -> Verdict:
    """Sort the candidate into a kind and a coverage verdict, in memory.

    Offline: it reads the recorded probe fields and nothing else. An **approved** candidate is
    left alone — the operator's decision stands, and the database constraint
    `source_candidate_not_coverable_never_approved` would refuse the contradiction anyway.
    """
    verdict = classify_probe(probe_facts(candidate))
    if candidate.status == SourceCandidate.Status.APPROVED:
        return verdict
    candidate.kind, candidate.coverage = verdict.kind, verdict.coverage
    return verdict


def apply_rules(candidate: SourceCandidate) -> str:
    """Apply offline ignore, hoster/social, onboarded and mirror rules in memory; return the
    reason."""
    if ignored(candidate.domain):
        note = ignore_reason(candidate.domain) or IGNORED_NOTE
        candidate.status = "ignored"
        add_note(candidate, note)
        return note
    kind, status = classify(candidate.domain)
    if status != "proposed":
        candidate.kind, candidate.status = kind, status
        record_coverage(candidate)
        return f"{kind} rule"
    if existing_site_domain(candidate.domain):
        mark_onboarded(candidate)
        return f"already onboarded as {candidate.domain}"
    if mirror := mirror_domain(candidate.domain):
        mark_mirror(candidate, mirror)
        return f"mirror of {mirror}"
    return ""


@dataclass(frozen=True)
class RuleChange:
    domain: str
    status: str
    kind: str
    reason: str
    coverage: str = ""


def reapply_rules(*, dry_run: bool = False) -> list[RuleChange]:
    """Re-evaluate offline rules for proposed candidates; reviewed rows are never changed."""
    changes: list[RuleChange] = []
    proposed = SourceCandidate.objects.filter(status="proposed")
    for pk in proposed.order_by("domain").values_list("pk", flat=True):
        with transaction.atomic():
            rows = proposed.filter(pk=pk)
            candidate = (rows if dry_run else rows.select_for_update()).first()
            if candidate is None:
                continue
            before = candidate.coverage
            reason = apply_rules(candidate)
            # A row the offline rules leave alone can still have a new coverage verdict: the
            # classifier's own rules change too (issue #118).
            if not reason:
                verdict = record_coverage(candidate)
                if candidate.coverage == before:
                    continue
                reason = f"coverage {before} -> {candidate.coverage}: {verdict.reason}"
            if not dry_run:
                candidate.save(update_fields=("status", "kind", "coverage", "notes", "evidence"))
            changes.append(
                RuleChange(
                    candidate.domain,
                    candidate.status,
                    candidate.kind,
                    reason,
                    candidate.coverage,
                )
            )
    return changes


@transaction.atomic
def upsert(
    url: str,
    *,
    query: Query | None = None,
    rank: int | None = None,
    title: str = "",
    page: Page | None = None,
) -> tuple[SourceCandidate | None, bool]:
    domain = normalize_domain(url)
    if not domain or ignored(domain) or domain in excluded_domains():
        return None, False
    kind, status = classify(domain)
    candidate, created = SourceCandidate.objects.get_or_create(
        domain=domain, defaults={"kind": kind, "status": status}
    )
    candidate = SourceCandidate.objects.select_for_update().get(pk=candidate.pk)
    if candidate.status == "proposed" and (mirror := mirror_domain(domain)):
        mark_mirror(candidate, mirror)
    evidence = candidate.evidence
    if query is not None:
        candidate.search_hits += 1
        if rank is not None:
            candidate.best_rank = min(candidate.best_rank or rank, rank)
        sample = {"query": query.text, "rank": rank, "url": url, "title": title}
        samples = evidence.setdefault("queries", [])
        if sample not in samples:
            samples.append(sample)
        evidence["queries"] = samples[-EVIDENCE_LIMIT:]
        if query.creator_id is not None:
            CandidateCreator.objects.get_or_create(candidate=candidate, creator_id=query.creator_id)
        candidate.distinct_creators = CandidateCreator.objects.filter(candidate=candidate).count()
    if page is not None:
        CandidatePage.objects.get_or_create(candidate=candidate, page=page)
        candidate.outlink_pages = CandidatePage.objects.filter(candidate=candidate).count()
        samples = evidence.setdefault("linking_pages", [])
        if page.url not in samples:
            samples.append(page.url)
        evidence["linking_pages"] = samples[-EVIDENCE_LIMIT:]
    candidate.evidence = evidence
    record_coverage(candidate)
    candidate.save()
    return candidate, created


@dataclass(frozen=True)
class ResearchResult:
    domain: str
    candidate: SourceCandidate | None
    created: bool
    skipped: str = ""  # why no candidate was touched


@transaction.atomic
def record_research(value: str, note: str) -> ResearchResult:
    """A domain named by desk research (traffic rankings, court reports, press) for review.

    Offline: no search call and no request to the domain. The same ignore, existing-Site and
    mirror rules as search results apply; the note is kept as `evidence["research"]`, never as a
    search hit, and a reviewed candidate keeps its decision.
    """
    note = " ".join(note.split())
    if not note:
        raise ValueError("A research note is required")
    domain = normalize_domain(value)
    if not domain:
        return ResearchResult(value, None, False, "not a domain")
    if ignored(domain):
        return ResearchResult(domain, None, False, ignore_reason(domain) or IGNORED_NOTE)
    if domain in excluded_domains():
        return ResearchResult(domain, None, False, "already a Site")
    kind, status = classify(domain)
    candidate, created = SourceCandidate.objects.get_or_create(
        domain=domain, defaults={"kind": kind, "status": status}
    )
    candidate = SourceCandidate.objects.select_for_update().get(pk=candidate.pk)
    if candidate.status == "proposed" and (mirror := mirror_domain(domain)):
        mark_mirror(candidate, mirror)
    notes = candidate.evidence.setdefault("research", [])
    if note not in notes:
        notes.append(note)
    candidate.evidence["research"] = notes[-EVIDENCE_LIMIT:]
    record_coverage(candidate)
    candidate.save()
    return ResearchResult(domain, candidate, created)


def link_domains() -> dict[str, set[int]]:
    """Read-only preview, also usable before discovery migrations are applied."""
    excluded = excluded_domains()
    result: dict[str, set[int]] = {}
    for page in Page.objects.only("pk", "raw_extraction").iterator():
        links = page.raw_extraction.get("external_links", [])
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, str):
                continue
            domain = normalize_domain(link)
            if domain and not ignored(domain) and domain not in excluded:
                result.setdefault(domain, set()).add(page.pk)
    return result


def mine_links() -> tuple[int, int]:
    new = updated = 0
    for domain, page_ids in link_domains().items():
        is_new = False
        for page in Page.objects.filter(pk__in=page_ids).iterator():
            _, created = upsert(domain, page=page)
            is_new |= created
        new += int(is_new)
        updated += int(not is_new)
    return new, updated


ChannelKey = tuple[bool, str]  # (private_invite, identity)


@dataclass
class ChannelLinks:
    page_ids: set[int]
    post_ids: set[int]


def link_channels() -> dict[ChannelKey, ChannelLinks]:
    """Read-only: Telegram channels and invite links in stored external links.

    Safety-blocked pages are skipped: no links from a blocked page are followed (docs/safety.md).
    """
    result: dict[ChannelKey, ChannelLinks] = {}
    pages = Page.objects.exclude(safety_status=Page.SafetyStatus.BLOCKED)
    for page in pages.only("pk", "raw_extraction").order_by("pk").iterator():
        links = page.raw_extraction.get("external_links", [])
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, str) or not (parsed := parse_telegram_link(link)):
                continue
            entry = result.setdefault(
                (parsed.private_invite, parsed.identity), ChannelLinks(set(), set())
            )
            entry.page_ids.add(page.pk)
            if parsed.post_id is not None:
                entry.post_ids.add(parsed.post_id)
    return result


@transaction.atomic
def upsert_channel(key: ChannelKey, links: ChannelLinks) -> tuple[TelegramChannelCandidate, bool]:
    """Merge link evidence; counts are recomputed from relations, review decisions kept."""
    private_invite, identity = key
    status = TelegramChannelCandidate.Status
    candidate, created = TelegramChannelCandidate.objects.get_or_create(
        private_invite=private_invite,
        identity=identity,
        defaults={
            "status": status.IGNORED if private_invite else status.PROPOSED,
            "notes": INVITE_NOTE if private_invite else "",
        },
    )
    candidate = TelegramChannelCandidate.objects.select_for_update().get(pk=candidate.pk)
    TelegramChannelPage.objects.bulk_create(
        [TelegramChannelPage(candidate=candidate, page_id=pk) for pk in sorted(links.page_ids)],
        ignore_conflicts=True,
    )
    pages = Page.objects.filter(telegramchannelpage__candidate=candidate)
    candidate.linking_pages = pages.count()
    candidate.distinct_creators = (
        Creator.objects.filter(pages__telegramchannelpage__candidate=candidate).distinct().count()
    )
    domains = sorted(set(pages.values_list("site__domain", flat=True)))
    candidate.distinct_sites = len(domains)
    candidate.sites = domains[:EVIDENCE_LIMIT]
    candidate.sample_pages = list(
        pages.order_by("-first_seen", "-pk").values_list("url", flat=True)[:EVIDENCE_LIMIT]
    )
    post_ids = {int(post_id) for post_id in candidate.sample_post_ids} | links.post_ids
    candidate.sample_post_ids = sorted(post_ids, reverse=True)[:EVIDENCE_LIMIT]
    candidate.save()
    return candidate, created


@dataclass(frozen=True)
class ChannelCounts:
    channels: int = 0
    channels_new: int = 0
    invites: int = 0
    invites_new: int = 0


def mine_channels() -> ChannelCounts:
    """Store Telegram channel candidates from indexed links; offline, never enqueues crawls."""
    channels = channels_new = invites = invites_new = 0
    for key, links in link_channels().items():
        _, created = upsert_channel(key, links)
        if key[0]:
            invites, invites_new = invites + 1, invites_new + int(created)
        else:
            channels, channels_new = channels + 1, channels_new + int(created)
    return ChannelCounts(channels, channels_new, invites, invites_new)
