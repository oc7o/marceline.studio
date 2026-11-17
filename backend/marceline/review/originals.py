"""Creator originals (`matching.Reference`): fingerprint coverage, matches and leaks per original,
and the M2 miss reasons of confirmed leaks that detection did not find. Read-only; import stays
CLI-only (`add_reference`). No media and no page URLs in these responses."""

from collections import defaultdict
from datetime import datetime

from django.http import HttpRequest
from ninja import Field, Router, Schema, Status

from index.models import Page
from marceline.review.auth import staff_auth
from marceline.review.common import moment
from marceline.review.m2_summary import M2ReferenceResult, m2_summary
from marceline.review.references import pair_status
from marceline.review.schemas import ErrorOut
from matching.creators import creator_names
from matching.models import Reference, ReferenceLeak, ReferenceMatch
from matching.reference_counts import frame_positions_by_reference

router = Router(tags=["review"], auth=staff_auth)

MAX_MOMENTS = 20


class StatusCountsOut(Schema):
    proposed: int = 0
    confirmed: int = 0
    rejected: int = 0


class OriginalOut(Schema):
    id: int
    creator_id: int
    creator_name: str = Field(description="Primary creator (rights holder).")
    creator_names: list[str] = Field(
        description="Everyone performing in the original, the primary creator first."
    )
    title: str
    kind: str
    duration_s: float | None
    frame_fps: float | None
    created_at: datetime
    frames: int = Field(description="Sampled frame positions (orientations counted once).")
    shared_frames: int = Field(description="Frame positions marked shared (intros, outros).")
    matches_strong: int
    matches_weak: int
    matches: StatusCountsOut
    leaks: StatusCountsOut
    linked_clusters: int = Field(description="Distinct clusters linked to its matches.")


class OriginalPageOut(Schema):
    id: int
    site_id: int
    site: str
    title: str
    availability: str
    safety_status: str


class OriginalMatchOut(Schema):
    id: int
    status: str
    strength: str
    best_distance: int
    evidence_frames: int
    best_moment: str
    matched_moments: list[str]
    shared_moments: list[str]
    cluster_id: int | None


class OriginalLeakOut(Schema):
    id: int
    status: str
    source: str
    score: float


class OriginalPairOut(Schema):
    status: str = Field(description="Pair status as in the reference queue (conflict included).")
    page: OriginalPageOut
    match: OriginalMatchOut | None
    leak: OriginalLeakOut | None


class MissOut(Schema):
    page_id: int
    site: str
    title: str
    reason: str


class LinkedOut(Schema):
    page_id: int
    cluster_id: int
    via_page_id: int


class OriginalM2Out(Schema):
    confirmed: int
    found_strong: int
    found_weak: int
    found_linked: int
    false_matches: int
    reviewed_matches: int
    max_distance: int
    misses: list[MissOut]
    linked: list[LinkedOut]


class OriginalDetailOut(Schema):
    original: OriginalOut
    pairs: list[OriginalPairOut]
    m2: OriginalM2Out


def originals_out(references: list[Reference]) -> list[OriginalOut]:
    ids = [reference.pk for reference in references]
    positions = frame_positions_by_reference(ids)
    matches: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    clusters: dict[int, set[int]] = defaultdict(set)
    for reference_id, status, strength, cluster_id in ReferenceMatch.objects.filter(
        reference_id__in=ids
    ).values_list("reference_id", "status", "strength", "cluster_id"):
        matches[reference_id][status] += 1
        matches[reference_id][strength] += 1
        if cluster_id is not None:
            clusters[reference_id].add(cluster_id)
    leaks: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for reference_id, status in ReferenceLeak.objects.filter(reference_id__in=ids).values_list(
        "reference_id", "status"
    ):
        leaks[reference_id][status] += 1

    def counts(row: dict[str, int]) -> StatusCountsOut:
        return StatusCountsOut(
            proposed=row.get("proposed", 0),
            confirmed=row.get("confirmed", 0),
            rejected=row.get("rejected", 0),
        )

    return [
        OriginalOut(
            id=reference.pk,
            creator_id=reference.creator_id,
            creator_name=reference.creator.name,
            creator_names=creator_names(reference),
            title=reference.title,
            kind=reference.kind,
            duration_s=reference.duration_s,
            frame_fps=reference.frame_fps,
            created_at=reference.created_at,
            frames=positions.get(reference.pk, (0, 0))[0],
            shared_frames=positions.get(reference.pk, (0, 0))[1],
            matches_strong=matches[reference.pk].get("strong", 0),
            matches_weak=matches[reference.pk].get("weak", 0),
            matches=counts(matches[reference.pk]),
            leaks=counts(leaks[reference.pk]),
            linked_clusters=len(clusters[reference.pk]),
        )
        for reference in references
    ]


@router.get("", response=list[OriginalOut])
def originals(request: HttpRequest) -> list[OriginalOut]:
    """Every original by id, with fingerprint and review counts."""
    return originals_out(
        list(
            Reference.objects.select_related("creator").prefetch_related("creators").order_by("pk")
        )
    )


def moments(kind: str, values: object) -> list[str]:
    """Stored reference positions as labels; `kind` decides whether they are times at all."""
    positions = (
        sorted({value for value in values if isinstance(value, int | float)})
        if isinstance(values, list)
        else []
    )
    return [moment(kind, value) for value in positions[:MAX_MOMENTS]]


def page_out(page: Page) -> OriginalPageOut:
    return OriginalPageOut(
        id=page.pk,
        site_id=page.site_id,
        site=page.site.domain,
        title=page.title,
        availability=page.availability,
        safety_status=page.safety_status,
    )


STATUS_ORDER = {"conflict": 0, "confirmed": 1, "proposed": 2, "rejected": 3}


def pairs_out(reference: Reference) -> list[OriginalPairOut]:
    matches = {
        row.page_id: row
        for row in ReferenceMatch.objects.filter(reference=reference).select_related("page__site")
    }
    leaks = {
        row.page_id: row
        for row in ReferenceLeak.objects.filter(reference=reference).select_related("page__site")
    }
    pairs = []
    for page_id in matches.keys() | leaks.keys():
        match, leak = matches.get(page_id), leaks.get(page_id)
        page = match.page if match is not None else leaks[page_id].page
        pairs.append(
            OriginalPairOut(
                status=pair_status(leak.status if leak else None, match.status if match else None),
                page=page_out(page),
                match=None
                if match is None
                else OriginalMatchOut(
                    id=match.pk,
                    status=match.status,
                    strength=match.strength,
                    best_distance=match.best_distance,
                    evidence_frames=match.evidence_frames,
                    best_moment=moment(reference.kind, match.best_reference_timestamp_s),
                    matched_moments=moments(reference.kind, match.matched_seconds),
                    shared_moments=moments(reference.kind, match.shared_seconds),
                    cluster_id=match.cluster_id,
                ),
                leak=None
                if leak is None
                else OriginalLeakOut(
                    id=leak.pk, status=leak.status, source=leak.source, score=leak.score
                ),
            )
        )

    def sort_key(pair: OriginalPairOut) -> tuple[int, int, int, int]:
        strength = pair.match.strength if pair.match else None
        return (
            STATUS_ORDER.get(pair.status, 9),
            {"strong": 0, "weak": 1}.get(strength or "", 2),
            pair.match.best_distance if pair.match else 999,
            pair.page.id,
        )

    return sorted(pairs, key=sort_key)


def m2_out(result: M2ReferenceResult | None, max_distance: int) -> OriginalM2Out:
    if result is None:
        return OriginalM2Out(
            confirmed=0,
            found_strong=0,
            found_weak=0,
            found_linked=0,
            false_matches=0,
            reviewed_matches=0,
            max_distance=max_distance,
            misses=[],
            linked=[],
        )
    pages = Page.objects.select_related("site").in_bulk([miss.page_id for miss in result.misses])
    numbers = result.numbers
    return OriginalM2Out(
        confirmed=numbers.confirmed,
        found_strong=numbers.strong,
        found_weak=numbers.weak,
        found_linked=numbers.linked,
        false_matches=numbers.false,
        reviewed_matches=numbers.reviewed,
        max_distance=max_distance,
        misses=[
            MissOut(
                page_id=miss.page_id,
                site=page.site.domain if (page := pages.get(miss.page_id)) else "",
                title=page.title if page else "",
                reason=miss.reason,
            )
            for miss in result.misses
        ],
        linked=[
            LinkedOut(page_id=row.page_id, cluster_id=row.cluster_id, via_page_id=row.via_page_id)
            for row in result.linked
        ],
    )


@router.get("/{reference_id}", response={200: OriginalDetailOut, 404: ErrorOut})
def original_detail(
    request: HttpRequest, reference_id: int
) -> Status[OriginalDetailOut | ErrorOut]:
    """One original: all matches and leaks per page, and M2 miss reasons for its confirmed leaks."""
    selection = (
        Reference.objects.select_related("creator")
        .prefetch_related("creators")
        .filter(pk=reference_id)
    )
    reference = selection.first()
    if reference is None:
        return Status(404, ErrorOut(detail="Original not found"))
    result = m2_summary(selection)
    return Status(
        200,
        OriginalDetailOut(
            original=originals_out([reference])[0],
            pairs=pairs_out(reference),
            m2=m2_out(result.references.get(reference.pk), result.max_distance),
        ),
    )
