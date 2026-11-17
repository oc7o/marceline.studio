"""Which originals an organisation owns — the single definition every customer query uses.

Ownership is a column (issue #76, ADR 0031): `marceline.OriginalOwnership` holds one row per
assigned original, naming its organisation. It lives in the project app because `matching` may not
depend on `accounts`. Nothing else reads or writes that table, and nothing may filter originals by
user or membership.

Narrow by design:
- an original **without a row** (an operator or CLI import nobody assigned) belongs to nobody and is
  invisible to every customer until it is assigned deliberately. An upload is a claim, not proof
  of ownership, and an import is not even that;
- **membership never moves originals.** A member who leaves, changes organisation or is deleted
  takes nothing with them, and a staff account that happens to hold a membership sees exactly its
  organisation's rows — never what it uploaded for someone else. `Reference.uploaded_by` is audit
  data only;
- a user with no membership owns nothing — staff included. Staff use the review API; the customer
  view is not an administrative window into someone else's account;
- a lookup that resolves to nothing returns `None`, and the API turns that into 404, never 403.

**One original per file.** A re-fingerprinted video is several `Reference` rows with one sha256
(v0 and v1 grids, 1 and 2 fps; ADR 0022), but a customer owns one video. `owned_originals` groups
an organisation's references by sha256 into `OwnedOriginal`s; the newest reference on a current
pipeline is the representative whose title and metadata a customer sees.

**Customers name originals by public id, never by a primary key** (issue #77, ADR 0033). The id
belongs to the file as this organisation owns it (`marceline.OriginalPublicId`, keyed by
organisation and sha256), not to any one reference, so it survives a new representative and a
retired twin. `owned_original(organisation, public_id)` is the only lookup: a malformed id, an
unknown one and another organisation's all answer None, and the API turns that into one 404.

Writes come in two kinds. `register` records the owner chosen when an original is created (staff
upload, CLI import, or inherited from an earlier reference of the same file) and refuses to
overwrite. `reassign` is the deliberate change — `manage.py assign_originals` or the admin with
confirmation — and logs every row it moves.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from accounts.models import Membership, Organisation
from core import public_ids
from marceline.app.auth import RequestUser
from marceline.deletion import handover
from marceline.models import OriginalOwnership, OriginalPublicId
from matching.fingerprint import BROWSER_PIPELINE_V0, PIPELINE_V0
from matching.models import Reference

logger = logging.getLogger("marceline.ownership")

Via = OriginalOwnership.Via

#: Recipes a newer one replaced (ADR 0022, ADR 0019). A reference on one of them is never the
#: representative while its file has a reference on a current recipe.
SUPERSEDED_PIPELINES = frozenset({PIPELINE_V0, BROWSER_PIPELINE_V0})


@dataclass(frozen=True)
class OwnedOriginal:
    """One video or photo collection as its owner sees it: every reference of one file.

    `public_id` is the only id a customer sees (ADR 0033); `representative` gives the title and
    metadata; `members` are all the references of the file (the representative included, in
    primary-key order), and findings and the case file are read across all of them. `pk` (the
    representative's primary key) is internal: case rows and findings are keyed by it.
    """

    representative: Reference
    members: tuple[Reference, ...]
    public_id: str

    @property
    def pk(self) -> int:
        return self.representative.pk

    @property
    def member_ids(self) -> list[int]:
        return [member.pk for member in self.members]


def representative_rank(reference: Reference) -> tuple[bool, object, int]:
    """Current recipe first, then the newest reference, then the highest id: the largest wins."""
    return (reference.pipeline not in SUPERSEDED_PIPELINES, reference.created_at, reference.pk)


#: Public ids are generated per kind; this is the kind of a customer original.
ORIGINAL_ID_PREFIX = "orig"


def group_originals(
    references: Iterable[Reference], public_ids: Mapping[str, str]
) -> list[OwnedOriginal]:
    """These references grouped by file, in the order each file first appears.

    `public_ids` maps each file's sha256 to its public id (`public_ids_for`); a file without one is
    a programming error, never a silent empty id.
    """
    by_file: dict[str, list[Reference]] = {}
    for reference in references:
        by_file.setdefault(reference.sha256, []).append(reference)
    return [
        OwnedOriginal(
            representative=max(rows, key=representative_rank),
            members=tuple(sorted(rows, key=lambda row: row.pk)),
            public_id=public_ids[sha256],
        )
        for sha256, rows in by_file.items()
    ]


def public_ids_for(organisation: Organisation, digests: Iterable[str]) -> dict[str, str]:
    """The public id of each of these files as this organisation's originals, sha256 → id.

    Creates the ids that are missing (`ignore_conflicts`, so two concurrent readers end up with the
    same row). The writers below create them when an owner is set; this keeps a read correct for
    ownership rows written any other way (fixtures, a manual database fix).
    """
    wanted = set(digests)
    if not wanted:
        return {}
    known = dict(
        OriginalPublicId.objects.filter(organisation=organisation, sha256__in=wanted).values_list(
            "sha256", "public_id"
        )
    )
    missing = wanted - known.keys()
    if missing:
        OriginalPublicId.objects.bulk_create(
            [OriginalPublicId(organisation=organisation, sha256=sha256) for sha256 in missing],
            ignore_conflicts=True,
        )
        known.update(
            OriginalPublicId.objects.filter(
                organisation=organisation, sha256__in=missing
            ).values_list("sha256", "public_id")
        )
    return known


class Change(NamedTuple):
    """One original whose owner a `reassign` call set: before and after, `None` = unassigned."""

    reference_id: int
    before: Organisation | None
    after: Organisation | None

    @property
    def changed(self) -> bool:
        before = self.before.pk if self.before is not None else None
        after = self.after.pk if self.after is not None else None
        return before != after


def membership_of(user: RequestUser) -> Membership | None:
    """The user's place in an organisation, or None (anonymous, or not a customer)."""
    if not user.is_authenticated:
        return None
    return Membership.objects.select_related("organisation").filter(user_id=user.pk).first()


def organisation_of(user: RequestUser) -> Organisation | None:
    """The organisation this request is scoped to, or None."""
    membership = membership_of(user)
    return membership.organisation if membership is not None else None


def owned_references(organisation: Organisation | None) -> QuerySet[Reference]:
    """Every original this organisation owns. `None` (no organisation) owns nothing."""
    if organisation is None:
        return Reference.objects.none()
    return Reference.objects.filter(ownership__organisation=organisation)


def owned_reference(organisation: Organisation | None, reference_id: int) -> Reference | None:
    """One original, if this organisation owns it. Otherwise None — the caller answers 404."""
    return owned_references(organisation).filter(pk=reference_id).first()


def owned_originals(organisation: Organisation | None) -> list[OwnedOriginal]:
    """Every original this organisation owns, one per file, newest representative first."""
    if organisation is None:
        return []
    references = list(owned_references(organisation).order_by("pk"))
    originals = group_originals(
        references, public_ids_for(organisation, (row.sha256 for row in references))
    )
    return sorted(
        originals,
        key=lambda original: (original.representative.created_at, original.pk),
        reverse=True,
    )


def owned_original(organisation: Organisation | None, public_id: str) -> OwnedOriginal | None:
    """One original by its public id, if this organisation owns it now. Otherwise None.

    Malformed, unknown, another organisation's, and one this organisation owned before the file
    was moved away: all None, answered identically. A primary key is never an original's name.
    """
    if organisation is None or not public_ids.is_valid(ORIGINAL_ID_PREFIX, public_id):
        return None
    row = OriginalPublicId.objects.filter(organisation=organisation, public_id=public_id).first()
    if row is None:
        return None
    references = list(owned_references(organisation).filter(sha256=row.sha256).order_by("pk"))
    if not references:
        return None
    [original] = group_originals(references, {row.sha256: row.public_id})
    return original


def assigned_references() -> QuerySet[Reference]:
    """Every original some organisation owns — the ones any customer can see at all."""
    return Reference.objects.filter(ownership__isnull=False)


def organisation_named(name: str) -> Organisation:
    """The organisation with exactly this name, ignoring case (names are unique that way)."""
    organisation = Organisation.objects.filter(name__iexact=name.strip()).first()
    if organisation is None:
        raise ValueError(f"no organisation named {name!r}")
    return organisation


def owner_of(reference_id: int) -> Organisation | None:
    """The organisation owning this original, or None when it is unassigned."""
    row = OriginalOwnership.objects.select_related("organisation").filter(pk=reference_id).first()
    return row.organisation if row is not None else None


def inherited_owner(sha256: str) -> tuple[Organisation, Reference] | None:
    """The owner of earlier references of the same file, and the one it was read from.

    A re-fingerprinted original keeps its sha256, so a reference with that digest on another
    pipeline or sampling rate is the same video and its owner is authoritative — the same rule
    `import_fingerprints` applies to the cast. Unassigned twins contribute nothing. Two owned twins
    that disagree are an operator decision, never a guess: `ValueError`.
    """
    rows = list(
        OriginalOwnership.objects.select_related("organisation", "reference")
        .filter(reference__sha256=sha256)
        .order_by("reference_id")
    )
    if not rows:
        return None
    owners = {row.organisation_id for row in rows}
    if len(owners) > 1:
        described = ", ".join(f"{row.reference_id} -> {row.organisation.name}" for row in rows)
        raise ValueError(
            f"earlier references of this file belong to different organisations ({described}); "
            "settle it with assign_originals first"
        )
    return rows[0].organisation, rows[0].reference


def owner_for_new(
    sha256: str, chosen: Organisation | None
) -> tuple[Organisation | None, Reference | None]:
    """The owner of a new reference of this file, and the earlier reference it was inherited from.

    An explicit choice wins, but may not contradict the owner of an earlier reference of the same
    file (one file, one owner): that is a `ValueError`, settled with `assign_originals`. Without a
    choice the earlier owner is inherited — the caller must say so, never silently. Nothing either
    way leaves the original unassigned.
    """
    inherited = inherited_owner(sha256)
    if chosen is None:
        return (inherited[0], inherited[1]) if inherited is not None else (None, None)
    if inherited is not None and inherited[0].pk != chosen.pk:
        raise ValueError(
            f"reference {inherited[1].pk} of this file belongs to {describe(inherited[0])}, "
            f"not {chosen.name}; move it with assign_originals first"
        )
    return chosen, None


def register(
    reference: Reference,
    organisation: Organisation | None,
    *,
    via: str,
    by: User | None = None,
) -> None:
    """Record the owner chosen when this original was created. `None` leaves it unassigned.

    Refuses an original that already has an owner: changing one is `reassign`, never a side
    effect of registering.
    """
    if organisation is None:
        return
    if OriginalOwnership.objects.filter(pk=reference.pk).exists():
        raise ValueError(f"original {reference.pk} already has an owner; use assign_originals")
    OriginalOwnership.objects.create(
        reference=reference, organisation=organisation, assigned_via=via, assigned_by=by
    )
    public_ids_for(organisation, [reference.sha256])
    logger.info(
        "original %s registered for organisation %s (%s) via %s",
        reference.pk,
        organisation.pk,
        organisation.name,
        via,
    )


def with_twins(reference_ids: Iterable[int]) -> list[Reference]:
    """These originals plus every reference of the same files, in primary-key order.

    One file has one owner: moving a v1 re-fingerprint without its v0 twin would split one video
    between two organisations, and the next import of it could not tell which one to inherit.
    """
    named = list(Reference.objects.filter(pk__in=list(reference_ids)).values_list("sha256"))
    digests = {sha256 for (sha256,) in named}
    return list(Reference.objects.filter(sha256__in=digests).order_by("pk"))


@transaction.atomic
def reassign(
    references: Sequence[Reference],
    organisation: Organisation | None,
    *,
    via: str,
    by: User | None = None,
    dry_run: bool = False,
) -> list[Change]:
    """Deliberately set (or with `None`, clear) the owner of these originals; log every move.

    Rows already owned by `organisation` are reported unchanged and not touched.

    **The case file does not follow the original** (ADR 0045): it hangs off the `Reference`, which
    moves, so the new owner would read the previous owner's stamps the moment this commits.
    `marceline.deletion.handover` deletes it here and marks the previous owner's remaining
    per-original data for removal after `CUSTOMER_DATA_RETENTION_DAYS`; `sync_cases` rebuilds a
    case for the new owner from the match rows. A dry run writes none of that either, and
    `assign_originals` prints `handover.plan(...)` before it asks.
    """
    current = {
        row.reference_id: row
        for row in OriginalOwnership.objects.select_for_update(of=("self",))
        .select_related("organisation")
        .filter(reference__in=[reference.pk for reference in references])
    }
    changes = []
    for reference in references:
        row = current.get(reference.pk)
        change = Change(reference.pk, row.organisation if row is not None else None, organisation)
        changes.append(change)
        if not change.changed or dry_run:
            continue
        if organisation is None:
            OriginalOwnership.objects.filter(pk=reference.pk).delete()
        else:
            public_ids_for(organisation, [reference.sha256])
            OriginalOwnership.objects.update_or_create(
                reference=reference,
                defaults={
                    "organisation": organisation,
                    "assigned_via": via,
                    "assigned_by": by,
                    "assigned_at": timezone.now(),
                },
            )
        logger.info(
            "original %s reassigned from %s to %s via %s by %s",
            reference.pk,
            describe(change.before),
            describe(change.after),
            via,
            f"account {by.pk}" if by is not None else "the command line",
        )
    if not dry_run:
        handover.apply(handover.plan(losses(references, changes)))
    return changes


def losses(
    references: Sequence[Reference], changes: Sequence[Change]
) -> list[tuple[int, str, str, bool]]:
    """`(organisation id, name, sha256, now unassigned)` per file an organisation is losing.

    The input of `marceline.deletion.handover.plan`, so a dry run can print exactly what a real
    run would do. A file whose owner does not change, and one that had no owner, contribute
    nothing.
    """
    return sorted(
        {
            (change.before.pk, change.before.name, reference.sha256, change.after is None)
            for reference, change in zip(references, changes, strict=True)
            if change.changed and change.before is not None
        }
    )


def describe(organisation: Organisation | None) -> str:
    """`7 (Test Customer)`, or `unassigned`."""
    return "unassigned" if organisation is None else f"{organisation.pk} ({organisation.name})"
