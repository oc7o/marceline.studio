"""DB side of browser-capture triage (ADR 0016): collect sources, apply hard exclusions, refresh
the queue and compute budgets. Pure scoring lives in `matching.triage`.

Nothing here opens a browser or touches the network. `next_capture_batch` is only for a future
capture worker, which needs a separate operator decision.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q

from catalog.sources import AgeConfirmationError, parse_age_confirmation
from index.models import FrameHash, Media, Page
from index.services import page_media_allowed
from matching import metadata_candidates, triage
from matching.calibration import current_thresholds
from matching.creators import creator_ids as reference_creator_ids
from matching.duration import page_duration
from matching.leaks import active_cluster_pages, related_pages
from matching.models import BrowserCaptureCandidate, Reference, ReferenceLeak, ReferenceMatch
from matching.pdq import MIN_QUALITY
from matching.services import media_content_key, mirror_groups

# A failed capture is re-queued by the next refresh until it has this many attempts.
MAX_CAPTURE_ATTEMPTS = 3
TOP_SHOWN = 10
REASONS = tuple(BrowserCaptureCandidate.Reason.values)

# Settings (read with defaults; settings.py wires them from the same env names later).
ENABLED_SETTING = "BROWSER_CAPTURE_ENABLED"
PER_SITE_SETTING = "BROWSER_CAPTURE_PER_SITE_PER_DAY"
TOTAL_SETTING = "BROWSER_CAPTURE_TOTAL_PER_DAY"
WINDOW_SETTING = "BROWSER_CAPTURE_WINDOW_UTC"
CONCURRENCY_SETTING = "BROWSER_CAPTURE_MAX_CONCURRENCY"
# The third signal's switch (ADR 0040), read here because its output is now a capture source.
METADATA_SETTING = "MATCHING_METADATA_CANDIDATES_ENABLED"
DEFAULT_PER_SITE_PER_DAY = 5
DEFAULT_TOTAL_PER_DAY = 20
DEFAULT_WINDOW_UTC = "01:00-05:00"
DEFAULT_MAX_CONCURRENCY = 1


@dataclass(frozen=True)
class GateSignals:
    """Age-gate labels the crawler recorded, by page id and site id (matching may not import
    crawler; the scheduler supplies them). `checked=False`: nothing was supplied."""

    pages: Mapping[int, str] = field(default_factory=dict)
    sites: Mapping[int, str] = field(default_factory=dict)
    checked: bool = True


NO_GATE_SIGNALS = GateSignals(checked=False)


@dataclass(frozen=True)
class CaptureConfig:
    enabled: bool
    per_site_per_day: int
    total_per_day: int
    window: triage.CaptureWindow
    max_concurrency: int


def capture_config() -> CaptureConfig:
    config = CaptureConfig(
        enabled=bool(getattr(settings, ENABLED_SETTING, False)),
        per_site_per_day=int(getattr(settings, PER_SITE_SETTING, DEFAULT_PER_SITE_PER_DAY)),
        total_per_day=int(getattr(settings, TOTAL_SETTING, DEFAULT_TOTAL_PER_DAY)),
        window=triage.parse_window(str(getattr(settings, WINDOW_SETTING, DEFAULT_WINDOW_UTC))),
        max_concurrency=int(getattr(settings, CONCURRENCY_SETTING, DEFAULT_MAX_CONCURRENCY)),
    )
    if min(config.per_site_per_day, config.total_per_day, config.max_concurrency) < 0:
        raise ValueError("browser capture budgets must not be negative")
    return config


@dataclass
class Candidate:
    page: Page
    sources: list[triage.Source]
    eligible_frames: int
    basis: dict[str, Any]
    recapture: str | None = None

    @property
    def lead(self) -> triage.Source:
        return triage.leading(self.sources)

    @property
    def priority(self) -> float:
        return triage.combine_priority(
            (source.component for source in self.sources), self.eligible_frames
        )

    def signals(self, gate_checked: bool) -> dict[str, Any]:
        ordered = sorted(
            self.sources,
            key=lambda item: (-item.component, item.reason, item.reference_id or 0),
        )
        return {
            "sources": [source.as_json() for source in ordered],
            "eligible_frames": self.eligible_frames,
            "frame_poverty": round(triage.frame_poverty(self.eligible_frames), 4),
            "basis": self.basis,
            "recapture": self.recapture,
            "gate_checked": gate_checked,
        }


@dataclass
class Triage:
    candidates: list[Candidate]
    skips: Counter[str]
    excluded_pages: dict[int, str]
    gate_checked: bool


@dataclass(frozen=True)
class _Match:
    status: str
    band: str
    strength: str
    evidence_frames: int
    confidence: float


def _age_approved(page: Page) -> bool:
    try:
        return parse_age_confirmation(page.site.age_confirmation) is not None
    except AgeConfirmationError:
        return False


def _pending_media(page_ids: Iterable[int]) -> set[int]:
    return set(
        Media.objects.filter(
            page_id__in=list(page_ids), status__in=(Media.Status.PENDING, Media.Status.RUNNING)
        ).values_list("page_id", flat=True)
    )


def _page_codes(page: Page, gate_signals: GateSignals, pending: set[int]) -> list[str]:
    return triage.page_exclusions(
        triage.PageFacts(
            safety_status=page.safety_status,
            safety_matches=page.safety_matches,
            availability=page.availability,
            site_enabled=page.site.enabled,
            site_capture_enabled=page.site.capture_enabled,
            site_age_approved=_age_approved(page),
            url=page.url,
            raw_extraction=page.raw_extraction,
            page_gate=gate_signals.pages.get(page.pk, ""),
            site_gate=gate_signals.sites.get(page.site_id, ""),
            media_pending=page.pk in pending,
            page_active=page.active,
        )
    )


def media_keys(page_ids: Iterable[int]) -> dict[int, list[str]]:
    """Host-independent media identities per page (mirror/site03 keys, else host + path)."""
    keys: dict[int, set[str]] = defaultdict(set)
    for page_id, url in Media.objects.filter(page_id__in=list(page_ids)).values_list(
        "page_id", "url"
    ):
        key = media_content_key(url)
        keys[page_id].add(key.value if key else triage.normalize_url(url))
    return {page_id: sorted(values) for page_id, values in keys.items()}


def captured_bases(page_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Basis of the latest capture per page: captured rows, and rows the capture worker skipped
    (`skipped` with a `capture_run`), e.g. DRM or a block. Triage skips don't count."""
    rows = (
        BrowserCaptureCandidate.objects.filter(page_id__in=list(page_ids))
        .filter(
            Q(status=BrowserCaptureCandidate.Status.CAPTURED)
            | (Q(status=BrowserCaptureCandidate.Status.SKIPPED) & ~Q(capture_run={}))
        )
        .order_by("page_id", "decided_at", "pk")
        .values_list("page_id", "signals")
    )
    return {page_id: dict((signals or {}).get("basis") or {}) for page_id, signals in rows}


def metadata_enabled(override: bool | None = None) -> bool:
    """Whether the third signal aims captures (ADR 0040 amendment). Off by default."""
    if override is not None:
        return override
    return bool(getattr(settings, METADATA_SETTING, False))


def collect_candidates(
    gate_signals: GateSignals = NO_GATE_SIGNALS,
    *,
    extra_page_ids: Iterable[int] = (),
    metadata: bool | None = None,
) -> Triage:
    """Read-only: score every source, apply hard exclusions, rank best first.

    `extra_page_ids` (open queue rows) get page-level exclusions evaluated even without a source,
    so a refresh can tell "now excluded" (skip) from "basis gone" (expire).

    `metadata` overrides `MATCHING_METADATA_CANDIDATES_ENABLED` for one call (the command's
    `--metadata-candidates`), so the operator can see what the third signal would aim at without
    switching it on for the scheduler.
    """
    metadata_pairs = (
        metadata_candidates.collect_candidates().candidates if metadata_enabled(metadata) else []
    )
    high = current_thresholds().high
    references = {
        row.pk: row
        for row in Reference.objects.only("pk", "creator_id", "duration_s").prefetch_related(
            "creators"
        )
    }
    matches = {
        (reference_id, page_id): _Match(status, band, strength, evidence, confidence)
        for reference_id, page_id, status, band, strength, evidence, confidence in (
            ReferenceMatch.objects.values_list(
                "reference_id",
                "page_id",
                "status",
                "band",
                "strength",
                "evidence_frames",
                "confidence",
            )
        )
    }
    leaks = {
        (reference_id, page_id): status
        for reference_id, page_id, status in ReferenceLeak.objects.exclude(
            status=ReferenceLeak.Status.PROPOSED
        ).values_list("reference_id", "page_id", "status")
    }
    rejected = {pair for pair, status in leaks.items() if status == "rejected"} | {
        pair for pair, match in matches.items() if match.status == "rejected"
    }
    confirmed: dict[tuple[int, int], list[str]] = defaultdict(list)
    for pair, status in leaks.items():
        if status == "confirmed":
            confirmed[pair].append("leak")
    for pair, match in matches.items():
        if match.status == "confirmed":
            confirmed[pair].append("match")

    # Exact-duration triage follows the leak rule (ADR 0010, 2026-09-16): a page linked to any
    # performer of the original satisfies the creator link, not only the primary creator.
    references_by_creator: dict[int, list[Reference]] = defaultdict(list)
    for reference in references.values():
        if reference.duration_s is not None:
            for creator_id in reference_creator_ids(reference):
                references_by_creator[creator_id].append(reference)
    linked: dict[int, set[int]] = defaultdict(set)
    for page_id, creator_id in Page.creators.through.objects.filter(
        creator_id__in=list(references_by_creator)
    ).values_list("page_id", "creator_id"):
        linked[page_id].add(creator_id)

    memberships = [("cluster", active_cluster_pages()), ("mirror", mirror_groups())]
    related: dict[tuple[int, int], tuple[int, str]] = {}
    for reference_id in sorted(references):
        strong = {
            page_id
            for (rid, page_id), match in matches.items()
            if rid == reference_id and match.strength == "strong" and match.status != "rejected"
        }
        strong -= {page_id for (rid, page_id) in rejected if rid == reference_id}
        if not strong:
            continue
        for page_id, seed in related_pages(memberships, strong).items():
            if (reference_id, page_id) not in matches:
                related[(reference_id, page_id)] = seed

    source_page_ids = (
        {page_id for _, page_id in matches}
        | {page_id for _, page_id in confirmed}
        | {page_id for _, page_id in related}
        | set(linked)
        | {item.page_id for item in metadata_pairs}
    )
    all_ids = source_page_ids | set(extra_page_ids)
    pages = {page.pk: page for page in Page.objects.filter(pk__in=all_ids).select_related("site")}
    frames: Counter[int] = Counter(
        dict(
            FrameHash.objects.filter(quality__gte=MIN_QUALITY, media__page_id__in=all_ids)
            .values("media__page_id")
            .annotate(total=Count("id"))
            .values_list("media__page_id", "total")
        )
    )

    sources: dict[int, list[triage.Source]] = defaultdict(list)
    skips: Counter[str] = Counter()
    for (reference_id, page_id), match in sorted(matches.items()):
        page = pages.get(page_id)
        if page is None or (match.band != "review" and match.strength != "weak"):
            continue
        exact = triage.exact_duration(
            page_duration(page.raw_extraction), references[reference_id].duration_s
        )
        sources[page_id].append(
            triage.Source(
                triage.REVIEW_BAND_MATCH,
                reference_id,
                triage.review_match_component(match.confidence, high, exact),
                {
                    "band": match.band,
                    "strength": match.strength,
                    "confidence": round(match.confidence, 4),
                    "evidence_frames": match.evidence_frames,
                    "match_status": match.status,
                    "exact_duration": exact,
                },
            )
        )
    for (reference_id, page_id), tables in sorted(confirmed.items()):
        if page_id in pages and frames[page_id] < triage.FEW_FRAME_HASHES:
            sources[page_id].append(
                triage.Source(
                    triage.CONFIRMED_LEAK_WITHOUT_FRAMES,
                    reference_id,
                    triage.CONFIRMED_LEAK_WEIGHT,
                    {"confirmed_in": sorted(tables)},
                )
            )
    for (reference_id, page_id), (seed_page, kind) in sorted(related.items()):
        if page_id in pages:
            sources[page_id].append(
                triage.Source(
                    triage.LINKED_CLUSTER_MEMBER,
                    reference_id,
                    triage.LINKED_CLUSTER_WEIGHT,
                    {"seed_page_id": seed_page, "kind": kind},
                )
            )
    for page_id, creator_ids in sorted(linked.items()):
        page = pages.get(page_id)
        if page is None or frames[page_id] > triage.SINGLE_THUMB_MAX_FRAMES:
            continue
        duration = page_duration(page.raw_extraction)
        for creator_id in sorted(creator_ids):
            for reference in references_by_creator[creator_id]:
                if (reference.pk, page_id) in matches or duration is None:
                    continue
                if triage.exact_duration(duration, reference.duration_s):
                    sources[page_id].append(
                        triage.Source(
                            triage.EXACT_DURATION_SINGLE_THUMB,
                            reference.pk,
                            triage.EXACT_DURATION_WEIGHT,
                            {"duration_diff_s": round(duration - (reference.duration_s or 0), 2)},
                        )
                    )

    # The third signal (ADR 0040 amendment): the page's own stored text is consistent with an
    # original. It rests on nothing of ours, so it carries two conditions the other sources do not
    # (`triage.metadata_exclusion`) — the ordinary media gate must already allow this page, and
    # PDQ must not have had a proper look at it yet. Corroboration: the page shares a cluster or
    # mirror group with a page confirmed for the *same* original, which is a second, independent
    # signal pointing at it.
    corroborated: dict[tuple[int, int], tuple[int, str]] = {}
    for reference_id in sorted({item.reference_id for item in metadata_pairs}):
        seeds = {page_id for (rid, page_id) in confirmed if rid == reference_id}
        if not seeds:
            continue
        for page_id, seed in related_pages(memberships, seeds).items():
            corroborated[(reference_id, page_id)] = seed
    for item in metadata_pairs:
        page = pages.get(item.page_id)
        if page is None:
            continue
        code = triage.metadata_exclusion(
            media_gate_allows=page_media_allowed(page.safety_status, page.safety_score),
            eligible_frames=frames[item.page_id],
            pair_proposed=(item.reference_id, item.page_id) in matches
            or (item.reference_id, item.page_id) in confirmed,
        )
        if code is not None:
            skips[code] += 1
            continue
        corroboration = corroborated.get((item.reference_id, item.page_id))
        details: dict[str, Any] = {
            "metadata_reason": str(item.signal.reason),
            **item.signal.signals,
            "corroborated_by": None if corroboration is None else corroboration[1],
        }
        if corroboration is not None:
            details["seed_page_id"] = corroboration[0]
        sources[item.page_id].append(
            triage.Source(
                triage.METADATA_CANDIDATE,
                item.reference_id,
                triage.METADATA_CANDIDATE_WEIGHT
                if corroboration is None
                else triage.METADATA_CORROBORATED_WEIGHT,
                details,
            )
        )

    evaluated = set(sources) | (set(extra_page_ids) & set(pages))
    pending = _pending_media(evaluated)
    captured = captured_bases(evaluated)
    keys = media_keys(evaluated)
    excluded: dict[int, str] = {}
    candidates: list[Candidate] = []
    for page_id in sorted(evaluated):
        page = pages[page_id]
        codes = _page_codes(page, gate_signals, pending)
        basis = triage.capture_basis(
            page.url, page_duration(page.raw_extraction), keys.get(page_id, ())
        )
        recapture = None
        if page_id in captured:
            recapture = triage.changed_substantially(captured[page_id], basis)
            if recapture is None:
                codes.append(triage.ALREADY_CAPTURED)
        if codes:
            excluded[page_id] = codes[0]
            if page_id in sources:
                skips[codes[0]] += 1
            continue
        kept = []
        for source in sources.get(page_id, ()):
            pair = (source.reference_id or 0, page_id)
            pair_match = matches.get(pair)
            code = triage.pair_exclusion(
                rejected=pair in rejected,
                confirmed=pair in confirmed,
                band=pair_match.band if pair_match else None,
                evidence_frames=pair_match.evidence_frames if pair_match else 0,
            )
            if code is None:
                kept.append(source)
            else:
                skips[code] += 1
        if kept:
            candidates.append(Candidate(page, kept, frames[page_id], basis, recapture))
    candidates.sort(key=lambda item: (-item.priority, item.page.pk))
    return Triage(candidates, skips, excluded, gate_signals.checked)


@dataclass
class RefreshResult:
    triage: Triage
    counts: Counter[str]
    dry_run: bool


def refresh_candidates(
    now: datetime,
    *,
    gate_signals: GateSignals = NO_GATE_SIGNALS,
    limit: int | None = None,
    dry_run: bool = False,
    metadata: bool | None = None,
) -> RefreshResult:
    """Idempotent queue refresh. New candidates are queued, open rows get current priority and
    signals (failed rows below MAX_CAPTURE_ATTEMPTS are re-queued), open rows of excluded pages
    are skipped, open rows without a source (or below `limit`) expire. Captured, skipped and
    expired rows are never touched. A dry run writes nothing."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be positive")
    status = BrowserCaptureCandidate.Status
    counts: Counter[str] = Counter()
    with transaction.atomic():
        open_rows_query = BrowserCaptureCandidate.objects.filter(
            status__in=BrowserCaptureCandidate.OPEN_STATUSES
        )
        if not dry_run:
            open_rows_query = open_rows_query.select_for_update()
        open_rows = {row.page_id: row for row in open_rows_query}
        result = collect_candidates(
            gate_signals, extra_page_ids=open_rows.keys(), metadata=metadata
        )
        chosen = result.candidates if limit is None else result.candidates[:limit]
        below_limit = {candidate.page.pk for candidate in result.candidates[len(chosen) :]}
        for candidate in chosen:
            lead = candidate.lead
            values: dict[str, Any] = {
                "site_id": candidate.page.site_id,
                "reference_id": lead.reference_id,
                "reason": lead.reason,
                "priority": candidate.priority,
                "signals": candidate.signals(gate_signals.checked),
            }
            row = open_rows.pop(candidate.page.pk, None)
            if row is None:
                counts["created"] += 1
                if not dry_run:
                    BrowserCaptureCandidate.objects.create(
                        page=candidate.page, queued_at=now, **values
                    )
                continue
            changed = any(getattr(row, name) != value for name, value in values.items())
            requeue = row.status == status.FAILED and row.attempts < MAX_CAPTURE_ATTEMPTS
            counts["requeued" if requeue else "updated" if changed else "unchanged"] += 1
            if (changed or requeue) and not dry_run:
                for name, value in values.items():
                    setattr(row, name, value)
                if requeue:
                    row.status = status.QUEUED
                row.save()
        for page_id, row in open_rows.items():
            if page_id in result.excluded_pages:
                row.status = status.SKIPPED
                row.last_error = f"triage: {result.excluded_pages[page_id]}"
                counts["skipped"] += 1
            else:
                reason = "below --limit" if page_id in below_limit else "no capture source left"
                row.status, row.last_error = status.EXPIRED, f"triage: {reason}"
                counts["expired"] += 1
            if not dry_run:
                row.decided_at = now
                row.save()
    return RefreshResult(result, counts, dry_run)


@dataclass(frozen=True)
class BudgetView:
    config: CaptureConfig
    window_start: datetime
    window_end: datetime
    inside: bool
    used_total: int
    used_per_site: dict[int, int]
    queued: int


def budget_view(now: datetime) -> BudgetView:
    """Visits used in the window containing `now`, or the next window (budgets count per window,
    which is once a day). A visit is a row with an attempt decided since the window started."""
    config = capture_config()
    start, end, inside = triage.window_bounds(now.astimezone(UTC), config.window)
    visits = BrowserCaptureCandidate.objects.filter(attempts__gt=0, decided_at__gte=start)
    used_per_site = dict(
        visits.values("site_id").annotate(total=Count("id")).values_list("site_id", "total")
    )
    return BudgetView(
        config=config,
        window_start=start,
        window_end=end,
        inside=inside,
        used_total=sum(used_per_site.values()),
        used_per_site=used_per_site,
        queued=BrowserCaptureCandidate.objects.filter(
            status=BrowserCaptureCandidate.Status.QUEUED
        ).count(),
    )


def next_capture_batch(
    now: datetime, *, site_domain: str | None = None
) -> list[BrowserCaptureCandidate]:
    """Top queued candidates a capture worker may visit now: empty while capture is disabled or
    outside the window; otherwise within the total, per-site and concurrency budgets.

    Read-only. Safety, removal, site and gate-check conditions are re-checked here (defence in
    depth). The worker must claim rows itself (SKIP LOCKED), re-check the deny immediately before
    it opens a browser (`marceline.capture.guard`) and re-check robots and gates live.

    `site_domain` narrows the queue to one site, the way `crawl_worker --site` does. Budgets are
    unaffected: the totals still count every site's visits in the window.
    """
    view = budget_view(now)
    if not view.config.enabled or not view.inside:
        return []
    queryset = (
        BrowserCaptureCandidate.objects.filter(
            status=BrowserCaptureCandidate.Status.QUEUED,
            signals__gate_checked=True,
            page__safety_status=Page.SafetyStatus.CLEAR,
            page__safety_matches=[],
            site__enabled=True,
            site__capture_enabled=True,
        )
        .exclude(page__availability=Page.Availability.REMOVED)
        .select_related("page", "site", "reference")
        .order_by("-priority", "queued_at", "pk")
    )
    if site_domain:
        queryset = queryset.filter(site__domain=site_domain)
    rows = list(queryset)
    picked = triage.pick_batch(
        [triage.QueuedItem(row.pk, row.site_id) for row in rows],
        total_budget=view.config.total_per_day,
        per_site_budget=view.config.per_site_per_day,
        max_concurrency=view.config.max_concurrency,
        used_total=view.used_total,
        used_per_site=view.used_per_site,
    )
    by_id = {row.pk: row for row in rows}
    return [by_id[pk] for pk in picked]


def triage_report(result: RefreshResult, budget: BudgetView) -> list[str]:
    """Top-level summary lines; per-candidate details are indented (scheduler omits them)."""
    found = result.triage
    counts = result.counts
    lines = [
        f"Browser capture triage{' (dry run)' if result.dry_run else ''}: "
        f"{len(found.candidates)} candidates; "
        + ", ".join(
            f"{name} {counts[name]}"
            for name in ("created", "updated", "unchanged", "requeued", "expired", "skipped")
        )
    ]
    leading = Counter(candidate.lead.reason for candidate in found.candidates)
    per_source = Counter(
        source.reason for candidate in found.candidates for source in candidate.sources
    )
    lines.append(
        "Reasons (leading/sources): "
        + ", ".join(f"{reason}={leading[reason]}/{per_source[reason]}" for reason in REASONS)
    )
    exclusions = [
        f"{code}={found.skips[code]}"
        for code in (*triage.PAGE_EXCLUSIONS, *triage.PAIR_EXCLUSIONS, *triage.METADATA_EXCLUSIONS)
        if found.skips[code]
    ]
    lines.append("Exclusions: " + (", ".join(exclusions) or "none"))
    if not found.gate_checked:
        lines.append(
            "Gate signals: not supplied (crawler records are read by the scheduler job); "
            "rows refreshed now are not capturable until then"
        )
    lines.append(f"Top {TOP_SHOWN}:" if found.candidates else f"Top {TOP_SHOWN}: none")
    for candidate in found.candidates[:TOP_SHOWN]:
        lead = candidate.lead
        reference = f" ref {lead.reference_id}" if lead.reference_id is not None else ""
        lines.append(
            f"  page {candidate.page.pk} {candidate.page.site.domain} {lead.reason} "
            f"{candidate.priority:.4f}{reference}"
        )
    config = budget.config
    lines.append(
        f"Budget: capture {'enabled' if config.enabled else 'disabled'}; "
        f"{'current' if budget.inside else 'next'} window "
        f"{budget.window_start:%Y-%m-%d %H:%M}-{budget.window_end:%H:%M} UTC; "
        f"visits {budget.used_total}/{config.total_per_day}, "
        f"{config.per_site_per_day} per site, concurrency {config.max_concurrency}; "
        f"{budget.queued} queued"
    )
    for site_id, used in sorted(budget.used_per_site.items()):
        lines.append(f"  site {site_id}: {used}/{config.per_site_per_day} visits")
    return lines
