"""Cluster proposals queue. Membership edits go through `matching.services.move_member` and
`merge_clusters`, so the admin's rules apply; their `ValueError`s become 409 responses.

A cluster groups copies of one pirate video; most are not linked to a creator original. Links
come from `ReferenceMatch` / `ReferenceLeak` pairs on members (pair status as in the reference
queue). Nothing here decides automatically: `proposed` rows come from `build_clusters` evidence,
confirmed/rejected rows from people (admin, this UI, or SAME PairLabels imported as confirmed).
`ClusterMember` records no decider, so `provenance` is derived from evidence and PairLabels.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence

from django.db import IntegrityError, transaction
from django.db.models import Case, Exists, IntegerField, OuterRef, Prefetch, Q, Value, When
from django.http import HttpRequest
from ninja import Router, Status

from marceline.review.auth import staff_auth
from marceline.review.common import clamp_limit, media_refs, page_out
from marceline.review.pages import queue_recheck
from marceline.review.references import collect_pairs
from marceline.review.schemas import (
    ClusterFilter,
    ClusterMemberOut,
    ClusterOut,
    ClusterPageOut,
    ClusterRefOut,
    ConflictItemOut,
    ConflictOut,
    DecisionIn,
    ErrorOut,
    LinkedOriginalOut,
    LinkedPageOut,
    MediaRefOut,
    MemberDecisionOut,
    MergeIn,
    MoveIn,
    OriginalLinkOut,
    Provenance,
    RecheckOut,
    ReferenceOut,
)
from matching.creators import creator_names
from matching.duration import duration_tolerance, page_duration
from matching.models import Cluster, ClusterMember, PairLabel, Reference, ReferenceLeak
from matching.services import merge_clusters, move_member

router = Router(tags=["review"], auth=staff_auth)

PROPOSED = ClusterMember.Status.PROPOSED
CONFIRMED = ClusterMember.Status.CONFIRMED
REJECTED = ClusterMember.Status.REJECTED
TARGET_STATUS: dict[str, str] = {"confirm": CONFIRMED, "reject": REJECTED, "reset": PROPOSED}
STATUS_ORDER: dict[str, int] = {CONFIRMED: 0, PROPOSED: 1, REJECTED: 2}
PAIR_ORDER: dict[str, int] = {"confirmed": 0, "conflict": 1, "proposed": 2, "rejected": 3}
STRENGTH_ORDER: dict[str | None, int] = {"strong": 0, "weak": 1, None: 2}
MEDIA_PER_MEMBER = 4
DEFAULT_CLUSTERS = 10

LabelPairs = set[frozenset[int]]


def proposed_clusters() -> Exists:
    return Exists(ClusterMember.objects.filter(cluster_id=OuterRef("pk"), status=PROPOSED))


# --- Links to originals ---


References = dict[int, Reference]


def original_links(page_ids: Iterable[int]) -> tuple[dict[int, list[OriginalLinkOut]], References]:
    """Every (reference, page) pair of these pages, per page, best evidence first."""
    pairs = collect_pairs(Q(page_id__in=list(page_ids)))
    if not pairs:
        return {}, {}
    references = (
        Reference.objects.select_related("creator")
        .prefetch_related("creators")
        .in_bulk({pair.reference_id for pair in pairs.values()})
    )
    sources = dict(
        ReferenceLeak.objects.filter(
            pk__in=[pair.leak_id for pair in pairs.values() if pair.leak_id is not None]
        ).values_list("pk", "source")
    )
    links: dict[int, list[OriginalLinkOut]] = defaultdict(list)
    for pair in pairs.values():
        reference = references[pair.reference_id]
        links[pair.page_id].append(
            OriginalLinkOut(
                reference_id=reference.pk,
                reference_title=reference.title,
                creator_name=reference.creator.name,
                creator_names=creator_names(reference),
                status=pair.status,
                match_strength=pair.match_strength,
                match_status=pair.match_status,
                copy_extent=pair.match_copy_extent,
                leak_status=pair.leak_status,
                leak_source=sources.get(pair.leak_id) if pair.leak_id is not None else None,
            )
        )
    for page_links in links.values():
        page_links.sort(key=lambda link: (_link_rank(link), link.reference_id))
    return links, references


def _link_rank(link: OriginalLinkOut | LinkedPageOut) -> tuple[int, int]:
    return (PAIR_ORDER.get(link.status, 9), STRENGTH_ORDER.get(link.match_strength, 2))


def linked_originals(
    members: list[ClusterMember],
    links: dict[int, list[OriginalLinkOut]],
    references: References,
) -> list[LinkedOriginalOut]:
    """Originals linked through active members; rejected pairs do not link."""
    by_reference: dict[int, list[LinkedPageOut]] = {}
    for member in members:
        if member.status == REJECTED:
            continue
        for link in links.get(member.page_id, []):
            if link.status == "rejected":
                continue
            by_reference.setdefault(link.reference_id, []).append(
                LinkedPageOut(
                    page_id=member.page_id,
                    status=link.status,
                    match_strength=link.match_strength,
                    copy_extent=link.copy_extent,
                    leak_status=link.leak_status,
                    leak_source=link.leak_source,
                )
            )
    result = []
    for reference_id, pages in by_reference.items():
        pages.sort(key=lambda page: (*_link_rank(page), page.page_id))
        reference = references[reference_id]
        result.append(
            LinkedOriginalOut(
                reference=ReferenceOut(
                    id=reference.pk,
                    title=reference.title,
                    creator_id=reference.creator_id,
                    creator_name=reference.creator.name,
                    creator_names=creator_names(reference),
                    duration_s=reference.duration_s,
                ),
                pages=pages,
            )
        )
    result.sort(key=lambda item: (*_link_rank(item.pages[0]), item.reference.id))
    return result


def linked_cluster_ids(reference_id: int | None = None) -> set[int]:
    """Clusters with an active member in a non-rejected pair (of `reference_id`, if given)."""
    q = Q(reference_id=reference_id) if reference_id is not None else Q()
    pages = {pair.page_id for pair in collect_pairs(q).values() if pair.status != "rejected"}
    return set(
        ClusterMember.objects.filter(page_id__in=pages)
        .exclude(status=REJECTED)
        .values_list("cluster_id", flat=True)
    )


# --- Provenance ---


def same_label_pairs(page_ids: Iterable[int]) -> LabelPairs:
    ids = list(page_ids)
    return {
        frozenset((a, b))
        for a, b in PairLabel.objects.filter(
            label=PairLabel.Label.SAME, page_a_id__in=ids, page_b_id__in=ids
        ).values_list("page_a_id", "page_b_id")
    }


def provenance(member: ClusterMember, cluster_pages: set[int], labels: LabelPairs) -> Provenance:
    """Derived: `build_clusters` only proposes, and imports SAME labels as confirmed + manual."""
    if member.status == PROPOSED:
        return "system"
    if (
        member.status == CONFIRMED
        and member.evidence == ClusterMember.Evidence.MANUAL
        and any(frozenset((member.page_id, other)) in labels for other in cluster_pages)
    ):
        return "pair_label"
    return "reviewer"


# --- Output ---


def cluster_out(
    cluster: Cluster,
    members: list[ClusterMember],
    links: tuple[dict[int, list[OriginalLinkOut]], References],
    labels: LabelPairs,
    media: dict[int, list[MediaRefOut]],
) -> ClusterOut:
    page_links, references = links
    active = [m for m in members if m.status != REJECTED]
    durations = [d for m in active if (d := page_duration(m.page.raw_extraction)) is not None]
    longest = max(durations, default=None)
    cluster_pages = {m.page_id for m in members}

    def possible_clip(member: ClusterMember) -> bool:
        seconds = page_duration(member.page.raw_extraction)
        return (
            seconds is not None
            and longest is not None
            and longest - seconds > duration_tolerance(longest)
        )

    ordered = sorted(members, key=lambda m: (STATUS_ORDER.get(m.status, 9), m.page_id))
    return ClusterOut(
        id=cluster.pk,
        member_count=len(active),
        proposed_count=sum(m.status == PROPOSED for m in members),
        borderline_count=sum(m.status == PROPOSED and m.borderline for m in members),
        linked_originals=linked_originals(ordered, page_links, references),
        members=[
            ClusterMemberOut(
                id=member.pk,
                page=page_out(member.page),
                status=member.status,
                evidence=member.evidence,
                provenance=provenance(member, cluster_pages - {member.page_id}, labels),
                min_distance=member.min_distance,
                borderline=member.borderline,
                possible_clip=possible_clip(member),
                originals=page_links.get(member.page_id, []),
                media=media.get(member.page_id, []),
            )
            for member in ordered
        ],
    )


def clusters_out(clusters: Sequence[Cluster]) -> list[ClusterOut]:
    members = {cluster.pk: list(cluster.members.all()) for cluster in clusters}
    page_ids = {m.page_id for rows in members.values() for m in rows}
    links = original_links(page_ids)
    labels = same_label_pairs(page_ids)
    # Once for the whole page of clusters, like the reference queue does: `media_refs` joins
    # `index_media` to `index_page`, and per cluster that was one such query per row shown.
    media = media_refs(
        (member.page for rows in members.values() for member in rows), MEDIA_PER_MEMBER
    )
    return [cluster_out(cluster, members[cluster.pk], links, labels, media) for cluster in clusters]


def encode_cursor(linked: int, cluster_id: int) -> str:
    return f"{linked}:{cluster_id}"


def decode_cursor(cursor: str) -> tuple[int, int] | None:
    try:
        linked, cluster_id = (int(part) for part in cursor.split(":"))
    except ValueError:
        return None
    return (linked, cluster_id) if linked in (0, 1) else None


@router.get("", response={200: ClusterPageOut, 400: ErrorOut})
def cluster_queue(
    request: HttpRequest,
    show: ClusterFilter = "all",
    reference_id: int | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_CLUSTERS,
) -> Status[ClusterPageOut | ErrorOut]:
    """Clusters with at least one proposed member: linked to an original first, then by id.

    `show=all` (default) lists every open cluster, linked ones first; `linked` keeps only clusters
    linked to an original — most clusters are not, so that filter alone can look almost empty
    (issue #2, operator 2026-09-17). `reference_id` keeps clusters linked to that original (and
    implies `linked`, since a cluster unlinked to any original cannot be linked to one in
    particular). `cursor` is the last item's `next_cursor` (`<linked 1|0>:<cluster id>`)."""
    after = decode_cursor(cursor) if cursor else None
    if cursor and after is None:
        return Status(400, ErrorOut(detail="Invalid cursor"))
    limit = clamp_limit(limit, DEFAULT_CLUSTERS)
    linked = linked_cluster_ids(reference_id)
    queryset = Cluster.objects.filter(proposed_clusters()).annotate(
        linked=Case(
            When(pk__in=linked, then=Value(1)), default=Value(0), output_field=IntegerField()
        )
    )
    if show == "linked" or reference_id is not None:
        queryset = queryset.filter(pk__in=linked)
    if after is not None:
        after_linked, after_id = after
        position = Q(linked=after_linked, pk__gt=after_id)
        if after_linked == 1:
            position |= Q(linked=0)
        queryset = queryset.filter(position)
    clusters = list(
        queryset.order_by("-linked", "pk").prefetch_related(
            Prefetch("members", queryset=ClusterMember.objects.select_related("page__site"))
        )[: limit + 1]
    )
    page = clusters[:limit]
    last = page[-1] if page else None
    next_cursor = (
        encode_cursor(int(last.pk in linked), last.pk)
        if last is not None and len(clusters) > limit
        else None
    )
    return Status(200, ClusterPageOut(items=clusters_out(page), next_cursor=next_cursor))


@router.post("/members/decide", response={200: MemberDecisionOut, 404: ErrorOut, 409: ConflictOut})
def decide_members(
    request: HttpRequest, payload: DecisionIn
) -> Status[MemberDecisionOut | ErrorOut | ConflictOut]:
    """Confirm / reject / reset members atomically. A decided member is never flipped to the
    opposite decision without a reset. `reset` puts a member back to proposed (undo)."""
    target = TARGET_STATUS[payload.decision]
    members = list(ClusterMember.objects.filter(pk__in=payload.ids).order_by("pk"))
    missing = sorted(set(payload.ids) - {m.pk for m in members})
    if missing:
        return Status(404, ErrorOut(detail=f"Unknown member ids: {', '.join(map(str, missing))}"))
    if payload.decision != "reset":
        conflicts = [
            ConflictItemOut(
                kind="member",
                id=m.pk,
                status=m.status,
                reason=f"already {m.status}; reset it before changing the decision",
            )
            for m in members
            if m.status not in (PROPOSED, target)
        ]
        if conflicts:
            return Status(
                409,
                ConflictOut(
                    detail="Some members already carry another decision", conflicts=conflicts
                ),
            )
    notes = reset_notes(members) if payload.decision == "reset" else []
    changed = []
    try:
        with transaction.atomic():
            for member in members:
                if member.status == target:
                    continue
                member.status = target
                move_member(member, member.cluster)
                changed.append(member.pk)
    except ValueError as exc:
        return Status(409, ConflictOut(detail=str(exc)))
    except IntegrityError:
        return Status(
            409, ConflictOut(detail="A page already has another active cluster membership")
        )
    return Status(
        200, MemberDecisionOut(decision=payload.decision, member_ids=changed, notes=notes)
    )


def reset_notes(members: list[ClusterMember]) -> list[str]:
    """Resets that the next `build_clusters` run undoes: it re-imports SAME PairLabels."""
    confirmed = [m for m in members if m.status == CONFIRMED]
    if not confirmed:
        return []
    cluster_pages: dict[int, set[int]] = defaultdict(set)
    for cluster_id, page_id in ClusterMember.objects.filter(
        cluster_id__in={m.cluster_id for m in confirmed}
    ).values_list("cluster_id", "page_id"):
        cluster_pages[cluster_id].add(page_id)
    labels = same_label_pairs(page for pages in cluster_pages.values() for page in pages)
    return [
        f"Member {m.pk} (page {m.page_id}) was confirmed from your SAME pair label; "
        "build_clusters confirms it again unless you change that label in the admin."
        for m in confirmed
        if provenance(m, cluster_pages[m.cluster_id] - {m.page_id}, labels) == "pair_label"
    ]


@router.post("/{cluster_id}/recheck", response={200: RecheckOut, 404: ErrorOut})
def recheck_cluster(request: HttpRequest, cluster_id: int) -> Status[RecheckOut | ErrorOut]:
    """Queue availability checks for the cluster's active (non-rejected) member pages."""
    if not Cluster.objects.filter(pk=cluster_id).exists():
        return Status(404, ErrorOut(detail="Unknown cluster"))
    page_ids = (
        ClusterMember.objects.filter(cluster_id=cluster_id)
        .exclude(status=REJECTED)
        .values_list("page_id", flat=True)
    )
    return Status(200, queue_recheck(list(page_ids)))


@router.post(
    "/members/{member_id}/move", response={200: ClusterRefOut, 404: ErrorOut, 409: ErrorOut}
)
def move(request: HttpRequest, member_id: int, payload: MoveIn) -> Status[ClusterRefOut | ErrorOut]:
    member = ClusterMember.objects.filter(pk=member_id).first()
    cluster = Cluster.objects.filter(pk=payload.cluster_id).first()
    if member is None or cluster is None:
        return Status(404, ErrorOut(detail="Unknown member or cluster"))
    try:
        move_member(member, cluster)
    except ValueError as exc:
        return Status(409, ErrorOut(detail=str(exc)))
    except IntegrityError:
        return Status(409, ErrorOut(detail="A page already has another active cluster membership"))
    count = cluster.members.exclude(status=REJECTED).count()
    return Status(200, ClusterRefOut(id=cluster.pk, member_count=count))


@router.post("/merge", response={200: ClusterRefOut, 404: ErrorOut, 409: ErrorOut})
def merge(request: HttpRequest, payload: MergeIn) -> Status[ClusterRefOut | ErrorOut]:
    """Merge source clusters into the target (`matching.services.merge_clusters`)."""
    source_ids = set(payload.source_ids) - {payload.target_id}
    if not source_ids:
        return Status(409, ErrorOut(detail="Choose at least one other cluster to merge"))
    clusters = Cluster.objects.in_bulk({payload.target_id, *source_ids})
    missing = sorted({payload.target_id, *source_ids} - clusters.keys())
    if missing:
        return Status(404, ErrorOut(detail=f"Unknown cluster ids: {', '.join(map(str, missing))}"))
    try:
        target = merge_clusters(clusters[payload.target_id], [clusters[i] for i in source_ids])
    except ValueError as exc:
        return Status(409, ErrorOut(detail=str(exc)))
    count = target.members.exclude(status=REJECTED).count()
    return Status(200, ClusterRefOut(id=target.pk, member_count=count))


def proposed_cluster_count() -> int:
    return Cluster.objects.filter(proposed_clusters()).count()
