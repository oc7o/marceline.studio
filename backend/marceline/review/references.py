"""M2 ground-truth queue: reference leak suggestions and reference matches, one item per
(reference, page) pair.

A pair's status follows `matching.m2`: confirmed in either table is ground truth, rejected in either
is a false match. Decisions apply to both rows of a pair so the tables stay consistent. Confirm and
reject never overwrite an opposite human decision (409; reset first).

Order (like `ReferenceMatch.objects.review_queue`, ADR 0015): proposed pairs first, then the match's
band high → review → low (pairs without a match last), confidence desc, ranking score desc,
reference, page. Low-band proposed pairs are hidden unless asked for; decided pairs never are.
"""

import math
import re
from dataclasses import dataclass
from functools import reduce
from operator import or_
from typing import cast

from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Status

from index.models import Page
from marceline import cache_purge
from marceline.review.auth import staff_auth
from marceline.review.common import (
    active_clusters,
    clamp_limit,
    media_allowed,
    media_refs,
    moment,
    page_out,
)
from marceline.review.schemas import (
    Band,
    BandThresholdsOut,
    CandidateSource,
    ConfidenceSignalOut,
    ConflictItemOut,
    ConflictOut,
    CopyExtentCode,
    DecidedBy,
    Decision,
    DecisionIn,
    ErrorOut,
    ExtentReasonCode,
    LeakOut,
    MatchOut,
    PairDecisionOut,
    PairStatus,
    ReferenceItemOut,
    ReferenceItemPageOut,
    ReferenceOut,
    ReferenceQueueMetaOut,
    ReferenceSummaryOut,
    RejectRemainingIn,
    SignalsOut,
    StatusFilter,
)
from matching.confidence import (
    COMMON_PENALTY,
    CORROBORATION_WEIGHT,
    DISTANCE_WEIGHT,
    EVIDENCE_WEIGHT,
    EXACT_DURATION_S,
    EXACT_DURATION_WEIGHT,
    NEAR_DURATION_WEIGHT,
    TIMELINE_NEUTRAL,
    TIMELINE_WEIGHT,
    Thresholds,
)
from matching.copy_extent import (
    CLASSIFIER_VERSION,
    CopyExtent,
    current_extent,
    requires_exception_review,
)
from matching.creators import creator_names, creators_of
from matching.leak_scoring import STRONG_MATCH_WEIGHT, WEAK_MATCH_WEIGHT, score_page
from matching.models import ConfidenceSettings, Reference, ReferenceLeak, ReferenceMatch

router = Router(tags=["review"], auth=staff_auth)

PROPOSED = "proposed"
CONFIRMED = "confirmed"
REJECTED = "rejected"
TARGET_STATUS: dict[str, str] = {"confirm": CONFIRMED, "reject": REJECTED, "reset": PROPOSED}
MEDIA_PER_ITEM = 12
LOW = ReferenceMatch.Band.LOW
BAND_ORDER: dict[str | None, int] = {"high": 0, "review": 1, "low": 2}
NO_MATCH_BAND_ORDER = 3

PairKey = tuple[int, int]
# (proposed 0 / decided 1, band order, -confidence, -rank, reference id, page id)
SortKey = tuple[int, int, float, float, int, int]


def pair_status(*statuses: str | None) -> PairStatus:
    decided = {status for status in statuses if status in (CONFIRMED, REJECTED)}
    if len(decided) > 1:
        return "conflict"
    if CONFIRMED in decided:
        return "confirmed"
    if REJECTED in decided:
        return "rejected"
    return "proposed"


@dataclass
class Pair:
    reference_id: int
    page_id: int
    leak_id: int | None = None
    leak_status: str | None = None
    leak_score: float = 0.0
    match_id: int | None = None
    match_status: str | None = None
    match_strength: str | None = None
    match_band: str | None = None
    match_confidence: float = 0.0
    match_decided_by: str = ""
    match_copy_extent: CopyExtentCode | None = None

    @property
    def status(self) -> PairStatus:
        return pair_status(self.leak_status, self.match_status)

    @property
    def rank(self) -> float:
        if self.leak_id is not None:
            value = self.leak_score
        elif self.match_strength == ReferenceMatch.Strength.STRONG:
            value = STRONG_MATCH_WEIGHT
        else:
            value = WEAK_MATCH_WEIGHT
        return round(value, 6)

    @property
    def decided_by(self) -> DecidedBy | None:
        """Who decided the pair: `auto` when the match row was decided automatically, otherwise
        `human` (leaks carry no author; unauthored decisions count as human, like matching)."""
        if self.status == "proposed":
            return None
        if self.match_decided_by == ReferenceMatch.DecidedBy.AUTO and self.match_status not in (
            None,
            PROPOSED,
        ):
            return "auto"
        return "human"

    @property
    def sort_key(self) -> SortKey:
        matched = self.match_id is not None
        return (
            0 if self.status == "proposed" else 1,
            BAND_ORDER.get(self.match_band, NO_MATCH_BAND_ORDER)
            if matched
            else NO_MATCH_BAND_ORDER,
            -round(self.match_confidence, 6) if matched else 0.0,
            -self.rank,
            self.reference_id,
            self.page_id,
        )


def scope(
    reference_id: int | None = None, creator_id: int | None = None, site_id: int | None = None
) -> Q:
    q = Q()
    if reference_id is not None:
        q &= Q(reference_id=reference_id)
    if creator_id is not None:
        q &= Q(reference__creator_id=creator_id)
    if site_id is not None:
        q &= Q(page__site_id=site_id)
    return q


def collect_pairs(q: Q) -> dict[PairKey, Pair]:
    """All (reference, page) pairs in scope from both tables, values only."""
    pairs: dict[PairKey, Pair] = {}
    for pk, reference_id, page_id, status, score in ReferenceLeak.objects.filter(q).values_list(
        "pk", "reference_id", "page_id", "status", "score"
    ):
        pair = pairs.setdefault((reference_id, page_id), Pair(reference_id, page_id))
        pair.leak_id, pair.leak_status, pair.leak_score = pk, status, score
    matches = ReferenceMatch.objects.filter(q).only(
        "pk",
        "reference_id",
        "page_id",
        "status",
        "strength",
        "band",
        "confidence",
        "decided_by",
        "copy_extent",
        "copy_extent_version",
    )
    for match in matches:
        pair = pairs.setdefault(
            (match.reference_id, match.page_id), Pair(match.reference_id, match.page_id)
        )
        pair.match_id, pair.match_status = match.pk, match.status
        pair.match_strength, pair.match_band = match.strength, match.band
        pair.match_confidence, pair.match_decided_by = match.confidence, match.decided_by
        pair.match_copy_extent = copy_extent_out(current_extent(match))
    return pairs


def encode_cursor(key: SortKey) -> str:
    return ":".join(repr(part) for part in key)


def decode_cursor(cursor: str) -> SortKey | None:
    try:
        status, band, confidence, rank, reference_id, page_id = cursor.split(":")
        key = (
            int(status),
            int(band),
            float(confidence),
            float(rank),
            int(reference_id),
            int(page_id),
        )
    except ValueError:
        return None
    return key if math.isfinite(key[2]) and math.isfinite(key[3]) else None


def band_visible(pair: Pair, bands: set[str], include_low: bool) -> bool:
    """With a band filter only matches in those bands (plus low with `include_low`); without one
    everything except low-band matches, unless `include_low`.

    Bands only sort and filter the *proposed* queue. A decided pair is always visible: matching
    re-bands every row on each run, and a human-confirmed leak whose confidence later dips under
    a threshold must not vanish from `status=confirmed` (or `all`) because of it."""
    if pair.status != "proposed":
        return True
    if pair.match_id is not None and pair.match_band == LOW and include_low:
        return True
    if bands:
        return pair.match_id is not None and pair.match_band in bands
    return pair.match_id is None or pair.match_band != LOW


def band_thresholds() -> BandThresholdsOut:
    row = ConfidenceSettings.objects.filter(pk=1).first()
    if row is None:
        defaults = Thresholds()
        return BandThresholdsOut(high=defaults.high, review=defaults.review, calibrated=False)
    return BandThresholdsOut(
        high=row.high_threshold, review=row.review_threshold, calibrated=row.calibrated
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _duration_value(contribution: float) -> str:
    if contribution >= EXACT_DURATION_WEIGHT:
        return f"within {EXACT_DURATION_S:g} s of the original"
    if contribution >= NEAR_DURATION_WEIGHT:
        return "near the original's duration (cluster tolerance)"
    return "no duration match (never a penalty)"


def confidence_breakdown(match: ReferenceMatch) -> list[ConfidenceSignalOut]:
    """Readable rows from the stored `confidence_signals` (known components first, in scoring
    order; unknown numeric components after them). Contributions are the stored values."""
    stored = match.confidence_signals if isinstance(match.confidence_signals, dict) else {}
    score = match.timeline_score
    loose = f" ({match.loose_frames} loose)" if match.loose_frames else ""
    known: dict[str, tuple[str, float, str]] = {
        "evidence": (
            "Evidence frames",
            EVIDENCE_WEIGHT,
            _plural(match.evidence_frames, "evidence frame") + loose,
        ),
        "distance": ("Best distance", DISTANCE_WEIGHT, f"Hamming distance {match.best_distance}"),
        "timeline": (
            "Timeline",
            TIMELINE_WEIGHT,
            f"score {score:.2f}"
            if score is not None
            else f"no frame order: neutral {TIMELINE_NEUTRAL:g}",
        ),
        "duration": ("Duration", EXACT_DURATION_WEIGHT, ""),
        "corroboration": (
            "Corroboration",
            CORROBORATION_WEIGHT,
            "cluster or mirror page with a strong match"
            if stored.get("corroborated") is True
            else "none",
        ),
        "common": ("Common frames", -COMMON_PENALTY, _plural(match.common_frames, "common frame")),
    }
    rows: list[ConfidenceSignalOut] = []
    numeric = {
        key: float(value)
        for key, value in stored.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }
    for key, (label, weight, value) in known.items():
        if key in numeric:
            if key == "duration":
                value = _duration_value(numeric[key])
            rows.append(
                ConfidenceSignalOut(
                    key=key, label=label, value=value, weight=weight, contribution=numeric[key]
                )
            )
    for key in sorted(numeric.keys() - known.keys()):
        rows.append(
            ConfidenceSignalOut(
                key=key,
                label=key.replace("_", " ").capitalize(),
                value="",
                weight=None,
                contribution=numeric[key],
            )
        )
    return rows


FRAMES_IN_ORDER = re.compile(r"(\d+)/(\d+) frames in order")


def ordered_frames(reason: str) -> int | None:
    """Ordered frames judged by `verify_timeline`, summed over its per-sequence reasons."""
    found = FRAMES_IN_ORDER.findall(reason)
    return sum(int(total) for _, total in found) if found else None


def decided_by_out(value: str) -> DecidedBy | None:
    if value == ReferenceMatch.DecidedBy.AUTO:
        return "auto"
    if value == ReferenceMatch.DecidedBy.HUMAN:
        return "human"
    return None


def copy_extent_out(extent: CopyExtent) -> CopyExtentCode:
    if extent == CopyExtent.FULL_COPY:
        return "full_copy"
    if extent == CopyExtent.EXCERPT:
        return "excerpt"
    if extent == CopyExtent.COMPILATION_CANDIDATE:
        return "compilation_candidate"
    return "unknown"


EXTENT_REASONS: dict[str, ExtentReasonCode] = {
    "photo_collection": "photo_collection",
    "no_reference_duration": "no_reference_duration",
    "other_originals": "other_originals",
    "no_timeline": "no_timeline",
    "weak_timeline": "weak_timeline",
    "page_longer": "page_longer",
    "page_shorter": "page_shorter",
    "full_span": "full_span",
    "narrow_span": "narrow_span",
    "inconclusive": "inconclusive",
}


def extent_reason_out(match: ReferenceMatch) -> ExtentReasonCode:
    """The stored reason; `not_classified` for a row another classifier version wrote."""
    if match.copy_extent_version != CLASSIFIER_VERSION:
        return "not_classified"
    return EXTENT_REASONS.get(match.copy_extent_reason, "not_classified")


def band_out(value: str) -> Band:
    if value == ReferenceMatch.Band.HIGH:
        return "high"
    if value == ReferenceMatch.Band.REVIEW:
        return "review"
    return "low"


def _match_out(match: ReferenceMatch, kind: str) -> MatchOut:
    """`kind` is the original's `Reference.kind`: a photo collection's positions are not times."""

    def moments(values: object) -> list[str]:
        if not isinstance(values, list):
            return []
        return [moment(kind, value) for value in values if isinstance(value, int | float)]

    return MatchOut(
        id=match.pk,
        status=match.status,
        strength=match.strength,
        best_distance=match.best_distance,
        evidence_frames=match.evidence_frames,
        matched_frames=match.matched_frames,
        page_frames=match.page_frames,
        best_moment=moment(kind, match.best_reference_timestamp_s),
        candidate_source=cast(CandidateSource, match.candidate_source),
        matched_moments=moments(match.matched_seconds),
        shared_moments=moments(match.shared_seconds),
        band=band_out(match.band),
        confidence=match.confidence,
        timeline_score=match.timeline_score,
        consistent_frames=match.consistent_frames,
        ordered_frames=ordered_frames(match.timeline_reason),
        timeline_reason=match.timeline_reason,
        loose_frames=match.loose_frames,
        common_frames=match.common_frames,
        common_seconds=moments(match.common_seconds),
        decided_by=decided_by_out(match.decided_by),
        confidence_signals=confidence_breakdown(match),
        copy_extent=copy_extent_out(current_extent(match)),
        copy_extent_reason=extent_reason_out(match),
        copy_span_s=match.copy_span_s,
        copy_coverage=match.copy_coverage,
        copy_other_originals=match.copy_other_originals,
        exception_review_required=requires_exception_review(match),
    )


def related_kind(leak: ReferenceLeak | None) -> str | None:
    signals = leak.signals if leak is not None and isinstance(leak.signals, dict) else {}
    kind = signals.get("related_kind")
    return kind if isinstance(kind, str) and kind else None


def related_page_id(leak: ReferenceLeak | None) -> int | None:
    signals = leak.signals if leak is not None and isinstance(leak.signals, dict) else {}
    value = signals.get("matched_page_id")
    return int(value) if isinstance(value, str | int) and str(value).isdigit() else None


def build_items(pairs: list[Pair]) -> list[ReferenceItemOut]:
    leaks = ReferenceLeak.objects.in_bulk([p.leak_id for p in pairs if p.leak_id is not None])
    matches = ReferenceMatch.objects.in_bulk([p.match_id for p in pairs if p.match_id is not None])
    references = (
        Reference.objects.select_related("creator")
        .prefetch_related("creators")
        .in_bulk({p.reference_id for p in pairs})
    )
    pages = (
        Page.objects.select_related("site")
        .prefetch_related("creators")
        .in_bulk({p.page_id for p in pairs})
    )
    clusters = active_clusters(pages.keys())
    media = media_refs(pages.values(), MEDIA_PER_ITEM)
    items = []
    for pair in pairs:
        reference, page = references[pair.reference_id], pages[pair.page_id]
        leak = leaks.get(pair.leak_id) if pair.leak_id is not None else None
        match = matches.get(pair.match_id) if pair.match_id is not None else None
        creator = reference.creator
        # Same rule as matching.leaks.rank_candidates, so the shown score matches the stored one:
        # every performer's names strip title tokens, and a link to any of them counts.
        performers = creators_of(reference)
        names = [
            name
            for row in performers
            for name in (row.name, *(alias for alias in row.aliases if isinstance(alias, str)))
        ]
        performer_ids = {row.pk for row in performers}
        linked = any(row.pk in performer_ids for row in page.creators.all())
        evidence = score_page(
            reference.title,
            reference.duration_s,
            page.title,
            page.raw_extraction,
            names,
            linked,
            match.strength if match is not None else None,
        )
        signals = evidence.signals

        def number(value: object) -> float | None:
            return float(value) if isinstance(value, int | float) else None

        items.append(
            ReferenceItemOut(
                key=f"r{pair.reference_id}-p{pair.page_id}",
                status=pair_status(leak.status if leak else None, match.status if match else None),
                reference=ReferenceOut(
                    id=reference.pk,
                    title=reference.title,
                    creator_id=creator.pk,
                    creator_name=creator.name,
                    creator_names=[row.name for row in performers],
                    duration_s=reference.duration_s,
                ),
                page=page_out(page),
                signals=SignalsOut(
                    # Stored ranking (includes cluster/mirror propagation); live for match-only.
                    score=round(leak.score if leak is not None else evidence.score, 4),
                    related_kind=related_kind(leak),
                    related_page_id=related_page_id(leak),
                    duration_diff_s=number(signals.get("duration_diff_s")),
                    duration_ratio=number(signals.get("duration_ratio")),
                    title_similarity=round(number(signals.get("title_similarity")) or 0.0, 4),
                    creator_linked=linked,
                    matched_strength=match.strength if match is not None else None,
                ),
                leak=LeakOut(
                    id=leak.pk,
                    status=leak.status,
                    source=leak.source,
                    score=leak.score,
                    note=leak.note,
                )
                if leak is not None
                else None,
                match=_match_out(match, reference.kind) if match is not None else None,
                cluster=clusters.get(page.pk),
                media=media.get(page.pk, []),
                media_hidden=not media_allowed(page),
            )
        )
    return items


@router.get("", response=list[ReferenceSummaryOut])
def reference_summaries(request: HttpRequest) -> list[ReferenceSummaryOut]:
    """References with pair counts per status, ordered by id (keys 1-9 in the UI)."""
    counts: dict[int, dict[str, int]] = {}
    for pair in collect_pairs(Q()).values():
        row = counts.setdefault(pair.reference_id, {})
        row[pair.status] = row.get(pair.status, 0) + 1
        if pair.status == "proposed" and pair.match_id is None:
            row["suggestions"] = row.get("suggestions", 0) + 1
    return [
        ReferenceSummaryOut(
            id=reference.pk,
            title=reference.title,
            creator_id=reference.creator_id,
            creator_name=reference.creator.name,
            creator_names=creator_names(reference),
            duration_s=reference.duration_s,
            proposed=counts.get(reference.pk, {}).get("proposed", 0),
            proposed_suggestions=counts.get(reference.pk, {}).get("suggestions", 0),
            confirmed=counts.get(reference.pk, {}).get("confirmed", 0),
            rejected=counts.get(reference.pk, {}).get("rejected", 0),
            conflict=counts.get(reference.pk, {}).get("conflict", 0),
        )
        for reference in Reference.objects.select_related("creator")
        .prefetch_related("creators")
        .order_by("pk")
    ]


@router.get("/items", response={200: ReferenceItemPageOut, 400: ErrorOut})
def reference_items(
    request: HttpRequest,
    status: StatusFilter = "proposed",
    reference_id: int | None = None,
    creator_id: int | None = None,
    site_id: int | None = None,
    cursor: str | None = None,
    limit: int = 20,
    hide_removed: bool = False,
    band: Query[list[Band] | None] = None,
    include_low: bool = False,
    decided_by: DecidedBy | None = None,
) -> Status[ReferenceItemPageOut | ErrorOut]:
    """Pairs ordered proposed first, then match band high → review → low (pairs without a match
    last), confidence desc, ranking score desc, reference, page. Keyset cursor over that order.

    Low-band matches are hidden unless `include_low` or `band=low`. `band` (repeatable) keeps only
    matches in those bands. `decided_by`: `auto` = the match row was decided automatically,
    `human` = any other decided pair. `hide_removed` leaves out pages the crawler found removed."""
    after = decode_cursor(cursor) if cursor else None
    if cursor and after is None:
        return Status(400, ErrorOut(detail="Invalid cursor"))
    limit = clamp_limit(limit)
    q = scope(reference_id, creator_id, site_id)
    if hide_removed:
        q &= ~Q(page__availability=Page.Availability.REMOVED)
    bands: set[str] = set(band or [])
    in_scope = [
        pair
        for pair in collect_pairs(q).values()
        if (status == "all" or pair.status == status)
        and (decided_by is None or pair.decided_by == decided_by)
    ]
    visible = [pair for pair in in_scope if band_visible(pair, bands, include_low)]
    hidden_low = sum(
        pair.match_id is not None
        and pair.match_band == LOW
        and not band_visible(pair, bands, include_low)
        for pair in in_scope
    )
    pairs = sorted(visible, key=lambda pair: pair.sort_key)
    if after is not None:
        pairs = [pair for pair in pairs if pair.sort_key > after]
    page = pairs[:limit]
    next_cursor = encode_cursor(page[-1].sort_key) if len(pairs) > limit else None
    meta = ReferenceQueueMetaOut(band_thresholds=band_thresholds(), hidden_low=hidden_low)
    return Status(
        200, ReferenceItemPageOut(items=build_items(page), next_cursor=next_cursor, meta=meta)
    )


@transaction.atomic
def apply_pair_decision(
    keys: set[PairKey], decision: Decision
) -> tuple[list[int], list[int], list[ConflictItemOut]]:
    """Set both rows of every pair to the decision's status. Returns changed leak ids, changed
    match ids and conflicts; with conflicts nothing is written."""
    if not keys:
        return [], [], []
    q = reduce(or_, (Q(reference_id=ref, page_id=page) for ref, page in sorted(keys)))
    # Lock order: leaks, then matches (same for every caller).
    leaks = list(ReferenceLeak.objects.select_for_update().filter(q).order_by("pk"))
    matches = list(ReferenceMatch.objects.select_for_update().filter(q).order_by("pk"))
    target = TARGET_STATUS[decision]
    if decision != "reset":
        conflicts = [
            ConflictItemOut(
                kind=kind,
                id=row.pk,
                status=row.status,
                reason=f"already {row.status}; reset it before changing the decision",
            )
            for kind, rows in (("leak", leaks), ("match", matches))
            for row in rows
            if row.status not in (PROPOSED, target)
        ]
        if conflicts:
            return [], [], conflicts
    now = timezone.now()
    leak_ids = [row.pk for row in leaks if row.status != target]
    match_ids = [row.pk for row in matches if row.status != target]
    ReferenceLeak.objects.filter(pk__in=leak_ids).update(status=target, updated_at=now)
    # `decided_at` is when *this* decision was taken (issue #142) and is what a notice draft
    # snapshots as its human-review date. A reset clears it: there is no decision to date.
    ReferenceMatch.objects.filter(pk__in=match_ids).update(
        status=target, updated_at=now, decided_at=None if decision == "reset" else now
    )
    if leak_ids or match_ids:
        # What customers see changed: expire the owning organisations' cached bundles on
        # production after this commits (ADR 0028). A no-op unless purging is configured.
        cache_purge.purge_decided_references({reference for reference, _ in keys})
    return leak_ids, match_ids, []


def _decide(
    model: type[ReferenceLeak] | type[ReferenceMatch], kind: str, payload: DecisionIn
) -> Status[PairDecisionOut | ErrorOut | ConflictOut]:
    rows = {
        pk: (reference_id, page_id)
        for pk, reference_id, page_id in model.objects.filter(pk__in=payload.ids).values_list(
            "pk", "reference_id", "page_id"
        )
    }
    missing = sorted(set(payload.ids) - rows.keys())
    if missing:
        return Status(404, ErrorOut(detail=f"Unknown {kind} ids: {', '.join(map(str, missing))}"))
    leak_ids, match_ids, conflicts = apply_pair_decision(set(rows.values()), payload.decision)
    if conflicts:
        return Status(
            409, ConflictOut(detail="Some rows already carry another decision", conflicts=conflicts)
        )
    return Status(
        200, PairDecisionOut(decision=payload.decision, leak_ids=leak_ids, match_ids=match_ids)
    )


DECISION_RESPONSES = {200: PairDecisionOut, 404: ErrorOut, 409: ConflictOut}


@router.post("/leaks/decide", response=DECISION_RESPONSES)
def decide_leaks(
    request: HttpRequest, payload: DecisionIn
) -> Status[PairDecisionOut | ErrorOut | ConflictOut]:
    """Confirm / reject / reset ReferenceLeak rows (and the ReferenceMatch of the same pair)."""
    return _decide(ReferenceLeak, "leak", payload)


@router.post("/matches/decide", response=DECISION_RESPONSES)
def decide_matches(
    request: HttpRequest, payload: DecisionIn
) -> Status[PairDecisionOut | ErrorOut | ConflictOut]:
    """Confirm / reject / reset ReferenceMatch rows (and the ReferenceLeak of the same pair)."""
    return _decide(ReferenceMatch, "match", payload)


@router.post("/{reference_id}/reject-remaining", response=DECISION_RESPONSES)
def reject_remaining(
    request: HttpRequest, reference_id: int, payload: RejectRemainingIn
) -> Status[PairDecisionOut | ErrorOut | ConflictOut]:
    """Reject every still-proposed pair of a reference. Pairs with a fingerprint match are kept
    unless `include_matches` is set."""
    if not Reference.objects.filter(pk=reference_id).exists():
        return Status(404, ErrorOut(detail="Unknown reference"))
    pairs = collect_pairs(scope(reference_id=reference_id, site_id=payload.site_id))
    keys = {
        key
        for key, pair in pairs.items()
        if pair.status == "proposed" and (payload.include_matches or pair.match_id is None)
    }
    leak_ids, match_ids, conflicts = apply_pair_decision(keys, "reject")
    if conflicts:
        return Status(409, ConflictOut(detail="Some rows changed meanwhile", conflicts=conflicts))
    return Status(200, PairDecisionOut(decision="reject", leak_ids=leak_ids, match_ids=match_ids))


def proposed_pair_count() -> int:
    return sum(pair.status == "proposed" for pair in collect_pairs(Q()).values())
