"""Marking a pilot ended, and marking one original for removal (ADR 0045, issue #85).

Marking writes a date and deletes nothing. It is deliberately **not** behind
`CUSTOMER_DATA_DELETION_ENABLED`: the flag guards the job that deletes, and the mark is what a dry
run reports on. Both marks are set by staff — the admin with a confirmation tick, or
`manage.py end_pilot` / `manage.py remove_original`, both of which default to a dry run.

Nothing here infers a date. There is no rule that turns a lapsed subscription, a last login or an
empty account into the end of a pilot: this is the one operation in the product that cannot be
undone (ADR 0045, "the clock is a date a human set").
"""

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Organisation
from marceline.deletion.models import OrganisationEnding, OriginalRemoval

logger = logging.getLogger("marceline.deletion")


def window() -> timedelta:
    """How long after the clock the data is deleted (`CUSTOMER_DATA_RETENTION_DAYS`, 30)."""
    return timedelta(days=int(settings.CUSTOMER_DATA_RETENTION_DAYS))


def due_at(clock: datetime) -> datetime:
    """The earliest moment data marked at `clock` may be deleted."""
    return clock + window()


def end_pilot(
    organisation: Organisation,
    *,
    ended_at: datetime,
    reason: str = "",
    by: User | None = None,
) -> tuple[OrganisationEnding, bool]:
    """Mark this organisation's pilot ended. Returns the row and whether it was created.

    Re-marking an organisation that is already marked moves `ended_at` — an operator correcting a
    date is the reason this is not refused — and logs the move. It never touches `deleted_at`.
    """
    row, created = OrganisationEnding.objects.get_or_create(
        organisation=organisation,
        defaults={"ended_at": ended_at, "reason": reason, "marked_by": by},
    )
    if not created and (row.ended_at != ended_at or (reason and row.reason != reason)):
        previous = row.ended_at
        row.ended_at = ended_at
        row.reason = reason or row.reason
        row.marked_by = by or row.marked_by
        row.marked_at = timezone.now()
        row.save(update_fields=("ended_at", "reason", "marked_by", "marked_at"))
        logger.info(
            "pilot end of organisation %s moved from %s to %s",
            organisation.pk,
            previous.date().isoformat(),
            ended_at.date().isoformat(),
        )
    elif created:
        logger.info(
            "organisation %s marked ended on %s; data deletion due %s",
            organisation.pk,
            ended_at.date().isoformat(),
            due_at(ended_at).date().isoformat(),
        )
    return row, created


def lift_ending(organisation: Organisation) -> bool:
    """Take back a pending ending mark. True when one was removed.

    Only a *pending* mark: a completed deletion is not undone by deleting its mark, and there is
    nothing to put back.
    """
    deleted, _ = OrganisationEnding.objects.filter(
        organisation=organisation, deleted_at__isnull=True
    ).delete()
    if deleted:
        logger.info("pilot end of organisation %s lifted", organisation.pk)
    return bool(deleted)


def remove_original(
    organisation: Organisation,
    sha256: str,
    *,
    removed_at: datetime,
    reason: str = OriginalRemoval.Reason.STAFF,
    note: str = "",
    by: User | None = None,
) -> tuple[OriginalRemoval, bool]:
    """Mark one work of this organisation for removal. Returns the row and whether it was created.

    An existing pending mark keeps its clock: marking the same file twice does not postpone its
    deletion, and does not bring it forward either.
    """
    row, created = OriginalRemoval.objects.get_or_create(
        organisation=organisation,
        sha256=sha256,
        defaults={"removed_at": removed_at, "reason": reason, "note": note, "marked_by": by},
    )
    if created:
        logger.info(
            "organisation %s marked file %s for removal (%s); due %s",
            organisation.pk,
            sha256[:12],
            reason,
            due_at(removed_at).date().isoformat(),
        )
    return row, created


def lift_removal(organisation: Organisation, sha256: str) -> bool:
    """Take back a pending removal mark for one file. True when one was removed."""
    deleted, _ = OriginalRemoval.objects.filter(
        organisation=organisation, sha256=sha256, deleted_at__isnull=True
    ).delete()
    if deleted:
        logger.info("removal mark of organisation %s for %s lifted", organisation.pk, sha256[:12])
    return bool(deleted)
