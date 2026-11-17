"""Read-only M2 measurement, using (reference, page) as the evaluation unit."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, fields

from django.db.models import QuerySet

from index.models import FrameHash, Page
from matching.creators import creator_label
from matching.hamming import HashIndex
from matching.models import ClusterMember, Reference
from matching.pdq import MIN_QUALITY
from matching.references import STRICT_MAX_DISTANCE

M2_RECALL_TARGET = 0.70
M2_FALSE_MATCH_LIMIT = 0.05


@dataclass
class Counts:
    confirmed: int = 0
    strong: int = 0
    weak: int = 0
    linked: int = 0
    false: int = 0
    reviewed: int = 0
    suggestions: int = 0
    matches: int = 0

    def recall(self, found: int) -> float:
        return 100 * found / self.confirmed if self.confirmed else 0

    def summary(self) -> str:
        rate = f"{100 * self.false / self.reviewed:.1f}%" if self.reviewed else "n/a"
        return (
            f"confirmed leaks={self.confirmed}; found strong={self.strong}, weak={self.weak}, "
            f"linked={self.linked}; recall strong={self.recall(self.strong):.1f}%, "
            f"strong+weak={self.recall(self.strong + self.weak):.1f}%, "
            f"strong+weak+linked={self.recall(self.strong + self.weak + self.linked):.1f}%; "
            f"false matches={self.false}/{self.reviewed} reviewed ({rate}); "
            f"unreviewed suggestions={self.suggestions}, matches={self.matches}"
        )


def linked_pages(pages: set[int], strong_pages: set[int]) -> dict[int, tuple[int, int]]:
    """Map pages to (cluster, strong page) when both are active members of the same cluster."""
    if not pages or not strong_pages:
        return {}
    active = ClusterMember.objects.exclude(status=ClusterMember.Status.REJECTED)
    seeds: dict[int, int] = {}
    for cluster_id, page_id in (
        active.filter(page_id__in=strong_pages)
        .order_by("page_id")
        .values_list("cluster_id", "page_id")
    ):
        seeds.setdefault(cluster_id, page_id)
    return {
        page_id: (cluster_id, seeds[cluster_id])
        for cluster_id, page_id in active.filter(
            page_id__in=pages, cluster_id__in=seeds.keys()
        ).values_list("cluster_id", "page_id")
    }


def miss_reason(
    page: Page,
    index: HashIndex,
    max_distance: int,
    frames: Sequence[FrameHash] | None = None,
) -> str:
    """Why this confirmed leak has no stored match. `frames` are the page's frame hashes;
    passing them in keeps a report over many pages from issuing one query per page."""
    prefix = "page removed; " if page.availability == "removed" else ""
    frames = list(FrameHash.objects.filter(media__page=page)) if frames is None else list(frames)
    if not frames:
        statuses = sorted(
            {
                "skipped expired"
                if media.status == "skipped" and "expir" in media.last_error.lower()
                else media.status
                for media in page.media.all()
            }
        )
        return prefix + "no FrameHash rows; media: " + (", ".join(statuses) or "none")
    eligible = [frame for frame in frames if frame.quality >= MIN_QUALITY]
    if not eligible:
        return prefix + f"all frames below quality {MIN_QUALITY} ({len(frames)} frames)"
    prefix += f"only {len(frames)} frames; " if len(frames) < 2 else f"{len(frames)} frames; "
    if not len(index):
        return prefix + "no quality-eligible non-shared reference frames"
    nearest = min(int(index.distances(bytes(frame.pdq)).min()) for frame in eligible)
    return (
        prefix
        + f"nearest distance to non-shared reference frame is {nearest} "
        + (
            f"(threshold {max_distance}; within threshold, rerun matching / inspect evidence)"
            if nearest <= max_distance
            else f"(above threshold {max_distance})"
        )
    )


def m2_report(
    references: QuerySet[Reference], *, max_distance: int = STRICT_MAX_DISTANCE
) -> list[str]:
    if not 0 <= max_distance <= 256:
        raise ValueError("max-distance must be between 0 and 256")
    lines = [f"M2: reference/page pairs; stored matches within distance {max_distance}."]
    total = Counts()
    # The staff overview runs this report on every request (the SPA refreshes it every 60 s), so
    # the per-reference reads are prefetched: without them one overview cost 148 queries and
    # 386 ms — 90 `leaks`/`matches` round trips plus one cast query per reference for
    # `creator_label`, which `creators_of` asks callers over a list to prefetch (measured
    # 2026-09-17 on the live index, 45 originals).
    for reference in references.prefetch_related("leaks", "matches", "creators"):
        leaks = list(reference.leaks.all())
        matches = list(reference.matches.all())
        confirmed = {row.page_id for row in leaks if row.status == "confirmed"} | {
            row.page_id for row in matches if row.status == "confirmed"
        }
        rejected = {row.page_id for row in leaks if row.status == "rejected"} | {
            row.page_id for row in matches if row.status == "rejected"
        }
        hits = {row.page_id: row for row in matches if row.best_distance <= max_distance}
        reviewed = {row.page_id for row in matches if row.page_id in confirmed | rejected}
        # A confirmed leak without its own hit is still found when detection reached it through
        # an active cluster membership shared with a current, non-rejected strong hit.
        strong_pages = {
            pk for pk, row in hits.items() if row.strength == "strong" and pk not in rejected
        }
        linked = linked_pages(confirmed - hits.keys(), strong_pages)
        counts = Counts(
            confirmed=len(confirmed),
            strong=sum(hits[pk].strength == "strong" for pk in confirmed & hits.keys()),
            weak=sum(hits[pk].strength == "weak" for pk in confirmed & hits.keys()),
            linked=len(linked),
            false=len(reviewed & rejected),
            reviewed=len(reviewed),
            suggestions=sum(row.status == "proposed" for row in leaks),
            matches=sum(row.status == "proposed" for row in matches),
        )
        lines.append(
            f"Reference {reference.pk} {creator_label(reference)} — {reference.title}: "
            + counts.summary()
        )
        for pk, (cluster_id, seed) in sorted(linked.items()):
            lines.append(
                f"  Linked page {pk}: active member of cluster {cluster_id} "
                f"with strong match page {seed}"
            )
        missing = confirmed - hits.keys() - linked.keys()
        if missing:
            index = HashIndex.from_pairs(
                (pk, bytes(pdq))
                for pk, pdq in reference.frames.filter(
                    shared=False, quality__gte=MIN_QUALITY
                ).values_list("pk", "pdq")
            )
            # `miss_reason` reads the page's media and frame hashes; both in one query each
            # for the whole reference instead of two per missed page.
            frames_of: dict[int, list[FrameHash]] = defaultdict(list)
            for frame in FrameHash.objects.filter(media__page_id__in=missing).select_related(
                "media"
            ):
                frames_of[frame.media.page_id].append(frame)
            for page in (
                Page.objects.filter(pk__in=missing).prefetch_related("media").order_by("pk")
            ):
                lines.append(
                    f"  Miss page {page.pk}: "
                    + miss_reason(page, index, max_distance, frames_of[page.pk])
                )
        for field in fields(total):
            setattr(total, field.name, getattr(total, field.name) + getattr(counts, field.name))
    lines.append("Total: " + total.summary())
    found = total.strong + total.weak + total.linked
    passed = (
        total.confirmed > 0
        and total.reviewed > 0
        and found / total.confirmed >= M2_RECALL_TARGET
        and total.false / total.reviewed <= M2_FALSE_MATCH_LIMIT
    )
    lines.append(
        f"M2 exit: {'PASS' if passed else 'FAIL'}; "
        f"sample={total.confirmed} confirmed leaks, {total.reviewed} reviewed matches; "
        f"targets strong+weak+linked recall ≥{M2_RECALL_TARGET:.0%} "
        f"(now {total.recall(found):.1f}%), false matches ≤{M2_FALSE_MATCH_LIMIT:.0%}"
        + ("; too few to judge" if total.confirmed < 10 else "")
    )
    return lines
