"""Read helpers shared by the review routers (no writes)."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet

from django.db.models import Count

from index.models import Media, Page
from index.services import gate_min_score, page_media_allowed
from marceline.review.schemas import ClusterRefOut, MediaRefOut, PageOut
from matching.duration import page_duration
from matching.models import ClusterMember, Reference

# Still images the passthrough may serve. A photo post's image is the work itself (issue #69);
# it is as viewable as a poster, and leaving it out would make photo-first sources invisible here.
IMAGE_KINDS = (Media.Kind.POSTER, Media.Kind.FRAME, Media.Kind.PHOTO)
# Order within one page: the image that stands for the whole page first (a photo *is* the page,
# a poster stands in for its video), then hover frames. Unknown kinds sort last.
_KIND_ORDER: dict[str, int] = {Media.Kind.PHOTO: 0, Media.Kind.POSTER: 1, Media.Kind.FRAME: 2}
MEDIA_SAFE_STATUS = Page.SafetyStatus.CLEAR
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def clamp_limit(limit: int, default: int = DEFAULT_LIMIT) -> int:
    return default if limit < 1 else min(limit, MAX_LIMIT)


def clock(seconds: float) -> str:
    """Seconds as mm:ss (minutes are not wrapped into hours, like the admin)."""
    whole = max(int(seconds), 0)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def moment(kind: str, position: float) -> str:
    """Where in the original: mm:ss for a video, `image N` for a photo collection (ADR 0020).

    A collection's stored positions are image ordinals, not seconds; rendering them as a time
    would invent a timeline it does not have.
    """
    if kind == Reference.Kind.PHOTOS:
        return f"image {max(int(position), 0)}"
    return clock(position)


def media_allowed(page: Page) -> bool:
    """Media of blocked, review or gated pages is never listed or served.

    docs/safety.md and ADR 0018: a page whose own text was never scored, or scored below the
    threshold, has no fetchable media — so the moderation UI neither lists nor serves it.
    """
    return page_media_allowed(page.safety_status, page.safety_score)


def page_out(page: Page) -> PageOut:
    return PageOut(
        id=page.pk,
        site=page.site.domain,
        title=page.title,
        url=page.url,
        duration_s=page_duration(page.raw_extraction),
        availability=page.availability,
        availability_checked_at=page.availability_checked_at,
        uploaded_at=page.uploaded_at,
        safety_status=page.safety_status,
    )


def media_refs(
    pages: Iterable[Page],
    per_page: int,
    priority: Mapping[int, AbstractSet[int]] | None = None,
) -> dict[int, list[MediaRefOut]]:
    """Viewable image ids of media-safe pages, photos and posters first. Other pages get none.

    `priority[page_id]` (existing media ids of that page) sorts to the very front, ahead of the
    usual kind order, so a caller that needs one specific media id included — and able to mark it
    — can rely on it surviving the `per_page` cut (common frames, issue #16 follow-up: "which
    frame of this page is the one that repeats").
    """
    priority = priority or {}
    page_ids = [page.pk for page in pages if media_allowed(page)]
    refs: dict[int, list[MediaRefOut]] = defaultdict(list)
    if not page_ids:
        return refs
    rows = (
        Media.objects.filter(
            page_id__in=page_ids,
            page__safety_status=MEDIA_SAFE_STATUS,
            page__safety_score__gte=gate_min_score(),
            kind__in=IMAGE_KINDS,
        )
        .order_by("page_id", "pk")
        .values_list("pk", "page_id", "kind")
    )
    ordered = sorted(
        rows,
        key=lambda row: (
            row[1],
            0 if row[0] in priority.get(row[1], ()) else 1,
            _KIND_ORDER.get(row[2], len(_KIND_ORDER)),
            row[0],
        ),
    )
    for pk, page_id, kind in ordered:
        if len(refs[page_id]) < per_page:
            refs[page_id].append(MediaRefOut(id=pk, kind=kind))
    return refs


def active_clusters(page_ids: Iterable[int]) -> dict[int, ClusterRefOut]:
    """Each page's active (non-rejected) cluster with its active member count."""
    memberships = dict(
        ClusterMember.objects.filter(page_id__in=list(page_ids))
        .exclude(status=ClusterMember.Status.REJECTED)
        .values_list("page_id", "cluster_id")
    )
    counts = dict(
        ClusterMember.objects.filter(cluster_id__in=set(memberships.values()))
        .exclude(status=ClusterMember.Status.REJECTED)
        .values("cluster_id")
        .annotate(total=Count("pk"))
        .values_list("cluster_id", "total")
    )
    return {
        page_id: ClusterRefOut(id=cluster_id, member_count=counts.get(cluster_id, 0))
        for page_id, cluster_id in memberships.items()
    }
