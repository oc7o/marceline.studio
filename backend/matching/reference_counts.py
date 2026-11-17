"""Per-original frame and match counters as scalar subqueries.

Counting them through joins is what a `Count("frames__frame_index", distinct=True)` annotation
does, and on real data that is the most expensive query in the staff surfaces: the reference rows
are multiplied by their frame-hash rows *and* their match rows, so 44 originals with 614k stored
frame hashes cost one 3.7 s sort. A scalar subquery per counter reads each side on its own index
and never fans out.

`positions` is a count of *frame positions*, not of rows: the eight dihedral orientations of one
sampled moment are one position (`reference_unique_frame_orientation`).
"""

from django.db.models import Count, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce

from matching.models import Reference, ReferenceFrameHash, ReferenceMatch


def reference_frame_positions(*, shared: bool | None = None) -> Coalesce:
    """Sampled frame positions of the outer `Reference`; `shared` filters to (non-)shared ones."""
    frames = ReferenceFrameHash.objects.filter(reference=OuterRef("pk"))
    if shared is not None:
        frames = frames.filter(shared=shared)
    counted = (
        frames.order_by()
        .values("reference")
        .annotate(positions=Count("frame_index", distinct=True))
        .values("positions")
    )
    return Coalesce(Subquery(counted), Value(0))


def reference_match_count() -> Coalesce:
    """Match rows of the outer `Reference`."""
    counted = (
        ReferenceMatch.objects.filter(reference=OuterRef("pk"))
        .order_by()
        .values("reference")
        .annotate(matches=Count("pk"))
        .values("matches")
    )
    return Coalesce(Subquery(counted), Value(0))


def frame_positions_by_reference(reference_ids: list[int]) -> dict[int, tuple[int, int]]:
    """`{reference_id: (positions, shared_positions)}` for the given originals, in one query."""
    rows = (
        Reference.objects.filter(pk__in=reference_ids)
        .annotate(
            positions=reference_frame_positions(),
            shared_positions=reference_frame_positions(shared=True),
        )
        .values_list("pk", "positions", "shared_positions")
    )
    return {pk: (positions, shared) for pk, positions, shared in rows}
