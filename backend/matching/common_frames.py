"""Common page frames across the index: studio intros, watermarks, generic cards (ADR 0015).

`find_common_frames` is pure: near-identical frames (<= `max_distance`) on pages of at least
`min_pages` distinct videos form a group. Pages of one video count once: an active cluster or a
recognized mirror group is one video, and two pages with matching durations that share at least
`COPY_MIN_SHARED_FRAMES` near-identical frame pairs are copies of one video — where a page holds
fewer frames than that, sharing all it has is enough (`copy_frames_required`), because two
poster-only pages can never share two. That keeps a real leak copied to many unclustered pages
from being mistaken for an intro. Pages whose group frames come
from the same file (identical sha256, also through a URL-key `MediaReuse`) count once as well: a
site that serves one placeholder poster on many video pages is one image, not a studio intro
(docs/research/studio-attribution.md).

Human-labeled groups are anchors: every frame within their radius joins them on each rebuild, so
labels (and studio attribution) follow new pages. Unlabeled proposals are recomputed; an existing
unlabeled group whose representative lies within the radius of a new proposal keeps its row.
Only confirmed groups (kind other than `not_common`) discount match evidence; a proposal waits for
a human.
"""

from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np
from django.db import transaction
from django.utils import timezone

from index.models import FrameHash, Page
from matching.duration import matching_durations, page_duration
from matching.hamming import HashIndex
from matching.models import ClusterMember, CommonFrameGroup, CommonFrameMember
from matching.pdq import MIN_QUALITY
from matching.services import mirror_groups

COMMON_MAX_DISTANCE = 8
COMMON_MIN_PAGES = 5
COPY_MIN_SHARED_FRAMES = 2
SAMPLE_PAGES = 10
# Groups that discount matching evidence: only ones a human confirmed. A proposal is just the
# daily rebuild's guess; letting it discount would silently weaken real leaks (an intro-heavy leak
# copied to many pages looks exactly like one) before anyone has looked at it.
DISCOUNTING_STATUSES = (CommonFrameGroup.Status.CONFIRMED,)


@dataclass(frozen=True)
class CandidateFrame:
    frame_id: int
    page_id: int
    site_id: int
    pdq: bytes
    # The media file's sha256 (or its URL-key reuse source's); "" when unknown. Frames of one
    # file on several pages are one occurrence.
    file_key: str = ""


@dataclass(frozen=True)
class Anchor:
    group_id: int
    pdq: bytes
    max_distance: int


@dataclass
class FrameGroup:
    representative_hash: bytes
    members: list[tuple[CandidateFrame, int]] = field(default_factory=list)
    anchor_id: int | None = None
    videos: int = 0

    @property
    def pages(self) -> set[int]:
        return {frame.page_id for frame, _ in self.members}

    @property
    def sites(self) -> set[int]:
        return {frame.site_id for frame, _ in self.members}


class CommonFrames(NamedTuple):
    anchored: list[FrameGroup]
    proposed: list[FrameGroup]
    # Candidate groups (as proposed without the same-file rule) none of whose frames made it into
    # a proposal once pages showing the same file count as one video.
    dropped_same_file: int


def copy_frames_required(first: int | None, second: int | None) -> int:
    """Shared near-identical frames that make two pages of one group copies of one video.

    `COPY_MIN_SHARED_FRAMES` normally, but never more frames than the poorer page *has*: 64 % of
    the index's frame hashes come from media with exactly one frame
    (`docs/matching/0061-common-frames-review.md` §2.1), so two poster-only pages can never share
    two frames and the copy rule could not fire at all where it is needed most — one video
    reposted on ten pages was counted as ten videos. Both pages of a group already share this
    group's own frame, so one is what the rule then asks for, and `matching_durations` still has
    to agree.
    """
    available = min(
        COPY_MIN_SHARED_FRAMES if first is None else first,
        COPY_MIN_SHARED_FRAMES if second is None else second,
    )
    return max(1, min(COPY_MIN_SHARED_FRAMES, available))


def count_videos(
    pages: Iterable[int],
    video_groups: Mapping[int, Hashable],
    durations: Mapping[int, float | None],
    page_links: Mapping[tuple[int, int], int],
    frame_counts: Mapping[int, int] | None = None,
) -> int:
    """Distinct videos among a group's pages (deterministic, non-transitive leader grouping).

    `frame_counts` is how many candidate frames each page has at all; a page missing from it is
    treated as having enough (`copy_frames_required`). Every page here shares this group's frame,
    which is why one shared frame is credited to each pair before `page_links` is consulted.
    """
    counts = frame_counts or {}
    leaders: list[int] = []
    seen: set[Hashable] = set()
    for page in sorted(set(pages)):
        key = video_groups.get(page)
        if key is not None and key in seen:
            continue
        copy = any(
            max(page_links.get((min(page, leader), max(page, leader)), 0), 1)
            >= copy_frames_required(counts.get(page), counts.get(leader))
            and matching_durations(durations.get(page), durations.get(leader))
            for leader in leaders
        )
        if key is not None:
            seen.add(key)
        if not copy:
            leaders.append(page)
    return len(leaders)


def same_file_groups(
    members: Iterable[tuple[CandidateFrame, int]], video_groups: Mapping[int, Hashable]
) -> dict[int, Hashable]:
    """Page -> video key for a group's pages, also joining pages whose member frames come from the
    same file (equal non-empty `file_key`). Transitive; pages with no join keep no key."""
    parent: dict[Hashable, Hashable] = {}

    def find(node: Hashable) -> Hashable:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: Hashable, b: Hashable) -> None:
        first, second = find(a), find(b)
        if first != second:
            parent[second] = first

    pages: set[int] = set()
    for frame, _ in members:
        page = ("page", frame.page_id)
        pages.add(frame.page_id)
        if frame.file_key:
            union(("file", frame.file_key), page)
    for page_id in pages:
        key = video_groups.get(page_id)
        if key is not None:
            union(("video", key), ("page", page_id))
    return {page_id: find(("page", page_id)) for page_id in pages if ("page", page_id) in parent}


def find_common_frames(
    frames: Sequence[CandidateFrame],
    anchors: Sequence[Anchor],
    video_groups: Mapping[int, Hashable],
    durations: Mapping[int, float | None],
    *,
    max_distance: int = COMMON_MAX_DISTANCE,
    min_pages: int = COMMON_MIN_PAGES,
) -> CommonFrames:
    """Anchored and proposed groups, and how many candidates the same-file rule dropped. Pure;
    each frame joins at most one group."""
    frames = sorted(frames, key=lambda frame: frame.frame_id)
    frame_counts: dict[int, int] = {}
    for frame in frames:
        frame_counts[frame.page_id] = frame_counts.get(frame.page_id, 0) + 1
    anchored = [FrameGroup(anchor.pdq, anchor_id=anchor.group_id) for anchor in anchors]
    remaining: list[CandidateFrame] = []
    if anchors:
        index = HashIndex(list(range(len(anchors))), [anchor.pdq for anchor in anchors])
        radius = max(anchor.max_distance for anchor in anchors)
        for frame, hits in zip(
            frames, index.query_many([f.pdq for f in frames], radius), strict=True
        ):
            eligible = [
                (d, anchors[i].group_id, i) for i, d in hits if d <= anchors[i].max_distance
            ]
            if eligible:
                distance, _, position = min(eligible)
                anchored[position].members.append((frame, distance))
            else:
                remaining.append(frame)
    else:
        remaining = list(frames)
    for group in anchored:
        group.videos = count_videos(
            group.pages,
            same_file_groups(group.members, video_groups),
            durations,
            {},
            frame_counts,
        )
    if not remaining:
        return CommonFrames(anchored, [], 0)

    count = len(remaining)
    firsts, seconds, distances = HashIndex(
        list(range(count)), [frame.pdq for frame in remaining]
    ).neighbor_pairs(max_distance)
    page_of = np.asarray([frame.page_id for frame in remaining], dtype=np.int64)
    first_pages, second_pages = page_of[firsts], page_of[seconds]
    cross = first_pages != second_pages
    low = np.minimum(first_pages[cross], second_pages[cross])
    high = np.maximum(first_pages[cross], second_pages[cross])
    page_links: dict[tuple[int, int], int] = {}
    if len(low):
        pairs, counts = np.unique(np.stack([low, high], axis=1), axis=0, return_counts=True)
        page_links = {
            (int(a), int(b)): int(n)
            for (a, b), n in zip(pairs, counts, strict=True)
            if n >= COPY_MIN_SHARED_FRAMES
        }

    source = np.concatenate([firsts, seconds])
    target = np.concatenate([seconds, firsts])
    weight = np.concatenate([distances, distances])
    order = np.lexsort((target, weight, source))
    source, target, weight = source[order], target[order], weight[order]
    starts = np.searchsorted(source, np.arange(count + 1))
    degree = np.diff(starts)
    leaders = sorted(
        (i for i in range(count) if degree[i] + 1 >= min_pages),
        key=lambda i: (-int(degree[i]), remaining[i].frame_id),
    )

    def propose(same_file: bool) -> list[FrameGroup]:
        assigned = np.zeros(count, dtype=bool)
        groups: list[FrameGroup] = []
        for leader in leaders:
            if assigned[leader]:
                continue
            neighbors = target[starts[leader] : starts[leader + 1]]
            near = weight[starts[leader] : starts[leader + 1]]
            free = ~assigned[neighbors]
            members = [(remaining[leader], 0)] + [
                (remaining[int(i)], int(d))
                for i, d in zip(neighbors[free], near[free], strict=True)
            ]
            group = FrameGroup(remaining[leader].pdq, members)
            if len(group.pages) < min_pages:
                continue
            keys = same_file_groups(members, video_groups) if same_file else video_groups
            group.videos = count_videos(group.pages, keys, durations, page_links, frame_counts)
            if group.videos < min_pages:
                continue
            assigned[leader] = True
            assigned[neighbors[free]] = True
            groups.append(group)
        groups.sort(key=lambda g: (-len(g.pages), min(f.frame_id for f, _ in g.members)))
        return groups

    proposed = propose(same_file=True)
    kept = {frame.frame_id for group in proposed for frame, _ in group.members}
    dropped = sum(
        not any(frame.frame_id in kept for frame, _ in group.members)
        for group in propose(same_file=False)
    )
    return CommonFrames(anchored, proposed, dropped)


def common_frame_ids(
    frames: Iterable[tuple[int, bytes]],
    groups: Sequence[tuple[bytes, int]],
    member_ids: Iterable[int] = (),
) -> set[int]:
    """Frame ids that discount evidence: stored members plus frames within a group's radius of
    its representative (covers frames hashed after the last `build_common_frames`)."""
    common = set(member_ids)
    if not groups:
        return common
    items = list(frames)
    index = HashIndex(list(range(len(groups))), [pdq for pdq, _ in groups])
    radius = max(distance for _, distance in groups)
    for (frame_id, _), hits in zip(
        items, index.query_many([pdq for _, pdq in items], radius), strict=True
    ):
        if any(d <= groups[i][1] for i, d in hits):
            common.add(frame_id)
    return common


def active_common_groups() -> tuple[list[tuple[bytes, int]], set[int]]:
    """(representative hash, radius) of discounting groups, and their stored member frame ids."""
    groups = CommonFrameGroup.objects.filter(status__in=DISCOUNTING_STATUSES).exclude(
        kind=CommonFrameGroup.Kind.NOT_COMMON
    )
    representatives = [
        (bytes(pdq), distance)
        for pdq, distance in groups.order_by("pk").values_list(
            "representative_hash", "max_distance"
        )
    ]
    members = set(
        CommonFrameMember.objects.filter(group__in=groups).values_list("frame_id", flat=True)
    )
    return representatives, members


def video_groups() -> dict[int, int]:
    """Page -> smallest page id of its video (active clusters and mirror groups, transitively)."""
    parent: dict[int, int] = {}

    def find(page: int) -> int:
        parent.setdefault(page, page)
        while parent[page] != page:
            parent[page] = parent[parent[page]]
            page = parent[page]
        return page

    def union(pages: Iterable[int]) -> None:
        roots = sorted({find(page) for page in pages})
        for root in roots[1:]:
            parent[root] = roots[0]

    clusters: dict[int, set[int]] = {}
    for cluster_id, page_id in ClusterMember.objects.exclude(
        status=ClusterMember.Status.REJECTED
    ).values_list("cluster_id", "page_id"):
        clusters.setdefault(cluster_id, set()).add(page_id)
    for pages in [*clusters.values(), *mirror_groups()]:
        union(pages)
    return {page: find(page) for page in list(parent)}


def load_candidate_frames(
    min_quality: int = MIN_QUALITY, *, include_inactive: bool = False
) -> list[CandidateFrame]:
    """Quality-eligible frames of active pages (issue #71).

    `include_inactive` widens the input to deactivated pages as well. It is for the labeling queue
    only: branding a site shows on untargeted pages is the same branding on targeted ones, and
    today both stored proposals sit on pages `deactivate_untargeted` later switched off
    (docs/matching/0061-common-frames-review.md). Discounting is unaffected either way — only a
    group a human confirmed ever discounts evidence.
    """
    page_filter = {} if include_inactive else {"media__page__active": True}
    return [
        # A URL-key reuse downloaded nothing, so its own sha256 is empty: its source's is the file.
        CandidateFrame(pk, page_id, site_id, bytes(pdq), sha256 or source_sha256 or "")
        for pk, page_id, site_id, pdq, sha256, source_sha256 in FrameHash.objects.filter(
            quality__gte=min_quality, **page_filter
        )
        .order_by("pk")
        .values_list(
            "pk",
            "media__page_id",
            "media__page__site_id",
            "pdq",
            "media__sha256",
            "media__reuse__source__sha256",
        )
        .iterator(chunk_size=5000)
    ]


def load_durations(pages: Iterable[int]) -> dict[int, float | None]:
    ids = sorted(set(pages))
    durations: dict[int, float | None] = {}
    for start in range(0, len(ids), 5000):
        for pk, raw in Page.objects.filter(pk__in=ids[start : start + 5000]).values_list(
            "pk", "raw_extraction"
        ):
            durations[pk] = page_duration(raw)
    return durations


def _refresh(group: CommonFrameGroup, found: FrameGroup) -> None:
    pages = sorted(found.pages)
    group.member_frame_count = len(found.members)
    group.page_count = len(pages)
    group.site_count = len(found.sites)
    group.sample_page_ids = pages[:SAMPLE_PAGES]


@transaction.atomic
def build_common_frames(
    *,
    max_distance: int = COMMON_MAX_DISTANCE,
    min_pages: int = COMMON_MIN_PAGES,
    min_quality: int = MIN_QUALITY,
    dry_run: bool = False,
    include_inactive: bool = False,
) -> list[str]:
    """Recompute common-frame groups and memberships; human labels survive. Idempotent."""
    if not 0 <= max_distance <= 64 or min_pages < 2 or not 0 <= min_quality <= 100:
        raise ValueError("max-distance must be 0-64, min-pages >= 2 and min-quality 0-100")
    existing = list(CommonFrameGroup.objects.select_for_update().order_by("pk"))
    labeled = [group for group in existing if group.labeled_at is not None]
    unlabeled = [group for group in existing if group.labeled_at is None]
    frames = load_candidate_frames(min_quality, include_inactive=include_inactive)
    anchored, proposed, dropped_same_file = find_common_frames(
        frames,
        [Anchor(g.pk, bytes(g.representative_hash), g.max_distance) for g in labeled],
        video_groups(),
        load_durations(frame.page_id for frame in frames),
        max_distance=max_distance,
        min_pages=min_pages,
    )
    CommonFrameMember.objects.all().delete()
    by_pk = {group.pk: group for group in labeled}
    rows: list[tuple[CommonFrameGroup, FrameGroup]] = []
    for found in anchored:
        assert found.anchor_id is not None
        rows.append((by_pk[found.anchor_id], found))

    reusable = HashIndex(
        list(range(len(unlabeled))), [bytes(g.representative_hash) for g in unlabeled]
    )
    reused: set[int] = set()
    created = 0
    for found in proposed:
        hits = [i for i, _ in reusable.query(found.representative_hash, max_distance)]
        position = next((i for i in hits if i not in reused), None)
        if position is None:
            group = CommonFrameGroup(
                representative_hash=found.representative_hash, max_distance=max_distance
            )
            created += 1
        else:
            reused.add(position)
            group = unlabeled[position]
            group.representative_hash = found.representative_hash
            group.max_distance = max_distance
        rows.append((group, found))
    stale = [group.pk for i, group in enumerate(unlabeled) if i not in reused]
    CommonFrameGroup.objects.filter(pk__in=stale).delete()
    members: list[CommonFrameMember] = []
    for group, found in rows:
        _refresh(group, found)
        group.save()
        members.extend(
            CommonFrameMember(
                group=group, frame_id=frame.frame_id, page_id=frame.page_id, distance=d
            )
            for frame, d in found.members
        )
    CommonFrameMember.objects.bulk_create(members, batch_size=5000)
    top = sorted((group for group, found in rows if found.anchor_id is None), key=_top_key)
    lines = [
        f"Common frames: {len(frames)} frames considered (quality >= {min_quality}, "
        f"{'active and inactive pages' if include_inactive else 'active pages'}), "
        f"max distance {max_distance}, min pages {min_pages}",
        f"Labeled groups kept: {len(anchored)} ({sum(len(g.members) for g in anchored)} frames)",
        f"Proposed groups: {len(proposed)} ({created} created, {len(reused)} kept, "
        f"{len(stale)} stale removed); {sum(len(g.members) for g in proposed)} frames on "
        f"{len(set().union(*(g.pages for g in proposed)))} pages",
        f"Dropped as one file on several pages (same sha256 counts once): {dropped_same_file} "
        "candidate groups",
    ]
    lines.extend(
        f"  group {group.pk}: {group.page_count} pages, {group.site_count} sites, "
        f"{group.member_frame_count} frames, sample pages {group.sample_page_ids}"
        for group in top[:10]
    )
    if dry_run:
        transaction.set_rollback(True)
        lines.append("Dry run: nothing written.")
    return lines


def _top_key(group: CommonFrameGroup) -> tuple[int, int]:
    return (-group.page_count, group.pk or 0)


def label_common_group(
    group: CommonFrameGroup,
    kind: str,
    studio_name: str,
    status: str,
    label: str | None = None,
) -> CommonFrameGroup:
    """Human label; labeled groups become rebuild anchors and keep their decision."""
    if kind not in CommonFrameGroup.Kind.values:
        raise ValueError(f"Unknown kind: {kind}")
    if status not in CommonFrameGroup.Status.values:
        raise ValueError(f"Unknown status: {status}")
    group.kind = kind
    group.studio_name = studio_name.strip()
    group.status = status
    if label is not None:
        group.label = label.strip()
    group.labeled_at = timezone.now()
    group.save()
    return group


def pages_showing(group: CommonFrameGroup) -> "list[int]":
    """Page ids with at least one member frame in the group (e.g. pages showing an intro)."""
    return sorted(set(group.members.values_list("page_id", flat=True)))
