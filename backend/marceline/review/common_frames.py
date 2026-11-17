"""Studio labeling queue for common-frame groups (issue #16, ADR 0015).

`build_common_frames` (`matching/common_frames.py`) proposes groups of near-identical page frames
shared by many unrelated videos — intros, logos, watermarks — but a proposal never discounts match
evidence by itself: only a group a human confirmed does
(`matching.common_frames.DISCOUNTING_STATUSES`, excluding `kind=not_common`). This router is that
missing human step, mirroring the admin's own `CommonFrameGroupAdmin` (same four fields,
`label_common_group`).

Sample pages reuse `marceline.review.common.page_out`/`media_refs`, the same helpers the reference
and cluster queues use, so a safety-flagged member's image is left out **server-side** by the
gate every other queue relies on, never by a client-side check. `media_gated` says when that is
why a sample has no image: a group whose pages all sit below the gate's score is one nobody can
judge by eye, and the screen has to say so rather than show an empty box (operator review
2026-09-18).

`member_frames` per sample page answers the operator's next complaint the same day: "I couldn't
review the group because I don't know which frames are the same." Each entry is that page's own
`matching.models.CommonFrameMember` row for this group — frame index, its order in the video where
one is knowable, the stored Hamming distance to the representative hash, and `showable`, which is
false for a sprite sheet, preview clip or browser capture: the passthrough
(`GET /media/{id}/image`) would serve the whole media row, not this one frame, so the screen must
not try. `media_refs` is asked to put a page's *showable* member media first (`priority`), so it
survives the per-page cap and load mode can mark it.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import cast

from django.db.models import Case, Count, IntegerField, Value, When
from django.http import HttpRequest
from ninja import Router, Status

from index.models import FrameHash, Media, Page
from marceline.review.auth import staff_auth
from marceline.review.common import IMAGE_KINDS, MAX_LIMIT, media_allowed, media_refs, page_out
from marceline.review.schemas import (
    CommonFrameGroupOut,
    CommonFrameKind,
    CommonFrameLabelIn,
    CommonFrameMemberFrameOut,
    CommonFrameMemberPageOut,
    CommonFrameQueueOut,
    CommonFrameShow,
    CommonFrameStatus,
    ErrorOut,
    MediaRefOut,
)
from matching.common_frames import DISCOUNTING_STATUSES, label_common_group
from matching.media_hash import PREVIEW_FPS
from matching.models import CommonFrameGroup, CommonFrameMember
from matching.timeline import Ordering, frame_position

router = Router(tags=["review"], auth=staff_auth)

PROPOSED = CommonFrameGroup.Status.PROPOSED
NOT_COMMON = CommonFrameGroup.Kind.NOT_COMMON
_STATUS_ORDER = Case(
    When(status=PROPOSED, then=Value(0)),
    When(status=CommonFrameGroup.Status.CONFIRMED, then=Value(1)),
    default=Value(2),
    output_field=IntegerField(),
)


def _discounting(group: CommonFrameGroup) -> bool:
    return group.status in DISCOUNTING_STATUSES and group.kind != NOT_COMMON


def _member_frame_out(
    *, media_id: int, media_kind: str, media_url: str, frame_index: int, distance: int
) -> CommonFrameMemberFrameOut:
    order = frame_position(media_kind, media_url, frame_index, media_id)
    fixed_rate = order is not None and order.ordering is Ordering.CLIP
    timestamp_s = (
        round(order.position / PREVIEW_FPS, 2)
        if fixed_rate and media_kind == Media.Kind.PREVIEW and order is not None
        else None
    )
    return CommonFrameMemberFrameOut(
        media_id=media_id,
        frame_index=frame_index,
        position=order.position if order is not None else None,
        timestamp_s=timestamp_s,
        distance=distance,
        showable=media_kind in IMAGE_KINDS,
        kind=media_kind,
    )


def _member_frames_and_counts(
    groups: Sequence[CommonFrameGroup], page_ids: Iterable[int]
) -> tuple[
    dict[tuple[int, int], list[CommonFrameMemberFrameOut]],
    dict[int, int],
    dict[int, set[int]],
]:
    """Per (group, page): its own member frames of that group. Per page: its total frame count,
    and the showable member media ids `media_refs` should put first (`priority`)."""
    page_ids = list(page_ids)
    member_frames: dict[tuple[int, int], list[CommonFrameMemberFrameOut]] = defaultdict(list)
    priority: dict[int, set[int]] = defaultdict(set)
    rows = CommonFrameMember.objects.filter(
        group_id__in=[group.pk for group in groups], page_id__in=page_ids
    ).values_list(
        "group_id",
        "page_id",
        "distance",
        "frame__frame_index",
        "frame__media_id",
        "frame__media__kind",
        "frame__media__url",
    )
    for group_id, page_id, distance, frame_index, media_id, media_kind, media_url in rows:
        out = _member_frame_out(
            media_id=media_id,
            media_kind=media_kind,
            media_url=media_url,
            frame_index=frame_index,
            distance=distance,
        )
        member_frames[(group_id, page_id)].append(out)
        if out.showable:
            priority[page_id].add(media_id)
    counts: dict[int, int] = dict(
        FrameHash.objects.filter(media__page_id__in=page_ids)
        .values("media__page_id")
        .annotate(total=Count("pk"))
        .values_list("media__page_id", "total")
    )
    return member_frames, counts, priority


def _member_page_out(
    page: Page,
    media: list[MediaRefOut],
    frames: list[CommonFrameMemberFrameOut],
    frame_count: int,
) -> CommonFrameMemberPageOut:
    ordered_frames = sorted(
        frames,
        key=lambda frame: (
            0 if frame.showable else 1,
            frame.position if frame.position is not None else 10**9,
            frame.frame_index,
        ),
    )
    return CommonFrameMemberPageOut(
        page=page_out(page),
        media=media,
        media_gated=not media_allowed(page),
        member_frames=ordered_frames,
        frame_count=frame_count,
    )


def _group_out(
    group: CommonFrameGroup,
    pages: dict[int, Page],
    media: dict[int, list[MediaRefOut]],
    member_frames: Mapping[tuple[int, int], list[CommonFrameMemberFrameOut]],
    frame_counts: Mapping[int, int],
) -> CommonFrameGroupOut:
    sample_ids = [pid for pid in group.sample_page_ids if isinstance(pid, int)]
    sample_pages: list[CommonFrameMemberPageOut] = []
    for page_id in sample_ids:
        page = pages.get(page_id)
        if page is None:
            continue
        sample_pages.append(
            _member_page_out(
                page,
                media.get(page_id, []),
                member_frames.get((group.pk, page_id), []),
                frame_counts.get(page_id, 0),
            )
        )
    return CommonFrameGroupOut(
        id=group.pk,
        kind=cast(CommonFrameKind, group.kind),
        status=cast(CommonFrameStatus, group.status),
        studio_name=group.studio_name,
        label=group.label,
        member_frame_count=group.member_frame_count,
        page_count=group.page_count,
        site_count=group.site_count,
        labeled_at=group.labeled_at,
        created_at=group.created_at,
        discounting=_discounting(group),
        sample_pages=sample_pages,
    )


def _groups_out(groups: Iterable[CommonFrameGroup]) -> list[CommonFrameGroupOut]:
    groups = list(groups)
    sample_ids = {pid for group in groups for pid in group.sample_page_ids if isinstance(pid, int)}
    pages = Page.objects.select_related("site").in_bulk(sample_ids)
    member_frames, frame_counts, priority = _member_frames_and_counts(groups, sample_ids)
    media = media_refs(pages.values(), len(sample_ids) or 1, priority=priority)
    return [_group_out(group, pages, media, member_frames, frame_counts) for group in groups]


@router.get("", response={200: CommonFrameQueueOut})
def common_frame_queue(
    request: HttpRequest, show: CommonFrameShow = "proposed", limit: int = MAX_LIMIT
) -> Status[CommonFrameQueueOut]:
    """Common-frame groups: `show=proposed` (default) is the actual queue — waiting for a label;
    `show=all` adds already-decided groups too, for review or undo. Proposed first, then
    confirmed, then rejected; largest (by page count) first within each."""
    limit = MAX_LIMIT if limit < 1 else min(limit, MAX_LIMIT)
    queryset = CommonFrameGroup.objects.all()
    if show == "proposed":
        queryset = queryset.filter(status=PROPOSED)
    rows = list(
        queryset.annotate(status_order=_STATUS_ORDER).order_by("status_order", "-page_count", "pk")[
            : limit + 1
        ]
    )
    page = rows[:limit]
    return Status(200, CommonFrameQueueOut(items=_groups_out(page), truncated=len(rows) > limit))


@router.post("/{group_id}/label", response={200: CommonFrameGroupOut, 404: ErrorOut})
def label_group(
    request: HttpRequest, group_id: int, payload: CommonFrameLabelIn
) -> Status[CommonFrameGroupOut | ErrorOut]:
    """Attach a studio label (or mark not common / reject): same fields and the same
    `label_common_group` service call as the admin's own form. A labeled group becomes a rebuild
    anchor (`labeled_at`); it only discounts match evidence once `status=confirmed` and
    `kind != not_common`."""
    group = CommonFrameGroup.objects.filter(pk=group_id).first()
    if group is None:
        return Status(404, ErrorOut(detail="Unknown common-frame group"))
    label_common_group(group, payload.kind, payload.studio_name, payload.status, payload.label)
    return Status(200, _groups_out([group])[0])


def proposed_common_frame_count() -> int:
    """For the navigation badge (`GET /review/counts`)."""
    return CommonFrameGroup.objects.filter(status=PROPOSED).count()
