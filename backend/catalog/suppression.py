"""Index suppression: names we must not index at all (docs/suppression.md).

A performer who is not our customer may tell us to stop indexing them. `catalog.CreatorSuppression`
is the durable marker for that, and this module is what the rest of the code asks about it:

- **read paths** go through `catalog.models.CreatorManager.usable()`, which excludes every
  suppressed creator, so nothing seeds, links, searches for or reports on the name;
- **write paths** that cannot use a queryset (the breadcrumb writers) filter their creator ids
  through `without_suppressed`;
- the last line of defence is a database trigger on the page-creator link table
  (`index/migrations/0021_suppressed_creator_links`), so even a writer nobody thought of stores no
  link.

Applying and lifting a suppression lives in `marceline/suppression.py`: it needs `index`,
`matching` and `accounts` together, which `catalog` may not import.
"""

from collections.abc import Iterable

from django.db.models import QuerySet

from catalog.models import Creator, CreatorSuppression


def active_suppressions() -> QuerySet[CreatorSuppression]:
    """Every suppression in force, newest request first."""
    return CreatorSuppression.objects.filter(lifted_at__isnull=True).select_related("creator")


def suppressed_creator_ids() -> set[int]:
    """Creator ids under an active suppression."""
    return set(
        CreatorSuppression.objects.filter(lifted_at__isnull=True).values_list(
            "creator_id", flat=True
        )
    )


def is_suppressed(creator: Creator | int) -> bool:
    """Is this creator (or creator id) under an active suppression?"""
    creator_id = creator.pk if isinstance(creator, Creator) else creator
    return CreatorSuppression.objects.filter(creator_id=creator_id, lifted_at__isnull=True).exists()


def suppression_of(creator: Creator | int) -> CreatorSuppression | None:
    """The suppression row of this creator, lifted or not — the record, for reports and lifts."""
    creator_id = creator.pk if isinstance(creator, Creator) else creator
    return CreatorSuppression.objects.filter(creator_id=creator_id).first()


def suppressed_names(names: Iterable[str]) -> list[str]:
    """Which of these creator names are suppressed, sorted. For commands that refuse by name.

    Never write `filter(suppression__lifted_at__isnull=True)` for this: that is a left join, so it
    also matches every creator with no suppression row at all.
    """
    wanted = sorted(set(names))
    if not wanted:
        return []
    return sorted(
        Creator.objects.filter(
            name__in=wanted,
            pk__in=CreatorSuppression.objects.filter(lifted_at__isnull=True).values("creator_id"),
        ).values_list("name", flat=True)
    )


def without_suppressed(creator_ids: Iterable[int]) -> list[int]:
    """These creator ids minus the suppressed ones, sorted. For writers that hold plain ids."""
    ids = sorted(set(creator_ids))
    if not ids:
        return []
    suppressed = set(
        CreatorSuppression.objects.filter(creator_id__in=ids, lifted_at__isnull=True).values_list(
            "creator_id", flat=True
        )
    )
    return [creator_id for creator_id in ids if creator_id not in suppressed]
