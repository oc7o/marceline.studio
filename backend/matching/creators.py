"""Everyone performing in an original: the primary creator plus co-performers.

`Reference.creator` stays the single primary link — the rights holder whose takedowns we file, and
what `matching` uses wherever exactly one creator is meant. `Reference.creators` holds everyone in
the scene, the primary creator included (ADR 0010, amendment "Several creators per original",
2026-09-16).

Nothing in the database keeps the two in sync: `creator` is a plain foreign key, `creators` a
separate join table, and Django's `save()` never writes a many-to-many. `set_creators` *is* that
invariant, and every import path calls it. The read helpers below tolerate a row whose join table
was never written (older data, a fixture, a test): they always put `reference.creator` first, so
the primary creator is reported even then.
"""

from collections.abc import Iterable

from catalog.models import Creator
from matching.models import Reference

# One label out of several names, e.g. "Sasha Meyer + Carla Bremer".
NAME_SEPARATOR = " + "


def set_creators(reference: Reference, creators: Iterable[Creator] = ()) -> None:
    """Link `creators` plus the primary creator to `reference`, replacing the current set.

    Call it inside the importing transaction, after the Reference row exists; `save()` does not
    and cannot do this for us.
    """
    reference.creators.set(sorted({creator.pk for creator in creators} | {reference.creator_id}))


def creators_of(reference: Reference) -> list[Creator]:
    """Primary creator first, then co-performers by name.

    Prefetch `creators` (and select `creator`) when calling this for a list of references.
    """
    others = sorted(
        (row for row in reference.creators.all() if row.pk != reference.creator_id),
        key=lambda row: row.name,
    )
    return [reference.creator, *others]


def creator_ids(reference: Reference) -> set[int]:
    """Ids of everyone in the original, primary included."""
    return {reference.creator_id, *(row.pk for row in reference.creators.all())}


def creator_names(reference: Reference) -> list[str]:
    """Names in the order of `creators_of`: the primary creator first."""
    return [row.name for row in creators_of(reference)]


def creator_label(reference: Reference) -> str:
    """One line for reports, admin and the UI: "Sasha Meyer + Carla Bremer"."""
    return NAME_SEPARATOR.join(creator_names(reference))
