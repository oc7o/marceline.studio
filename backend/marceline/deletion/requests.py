"""The creator's own request for an export or a deletion (ADR 0056, issue #133).

The only writer of `CustomerDataRequest`. Four operations and not one of them touches customer
data: `open_request` records that the creator asked, `withdraw` takes an open one back, `start` and
`settle` are what a staff member records while answering it, and `waiting` counts what is still
open for the daily operator report.

**Nothing here deletes or exports anything, by design.** ADR 0045's rule is unchanged: the clock
for a deletion is `OrganisationEnding.ended_at`, which a human sets with `manage.py end_pilot`, and
the export is produced by `manage.py export_customer_data`. A creator's request must therefore not
be able to start either — a button that deleted an organisation's data would be exactly the
inference ADR 0045 forbids, arriving from the least verified direction there is. What the creator
gets from the product is the thing that was missing: a recorded, dated, followable request, and the
one-month deadline of Art. 12(3) GDPR stated next to it.

One open request per organisation per kind, enforced by a partial unique index, so a creator who
presses the button twice has asked once. A refusal is `already_open`, which the screen reads as "we
have it" rather than as an error.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import Organisation
from marceline.deletion.models import CustomerDataRequest

logger = logging.getLogger("marceline.deletion")

Kind = CustomerDataRequest.Kind
Status = CustomerDataRequest.Status

#: Where a request is answered from, and the address both legal documents name (#133 point 1: the
#: service privacy notice C1 and the pilot agreement § 16 Abs. 7). A pinned constant rather than a
#: setting: it is a published promise, and the screen must never invent a second address. The
#: customer API hands it to both apps so no UI string carries it.
CONTACT_EMAIL = "contact@marceline.studio"

#: The word the creator types before a deletion request is accepted. Deliberately **not
#: translated**: the document a public host serves from the signed bundle is language-independent
#: (ADR 0028), and the confirmation has to be the same string whatever language the screen is in.
DELETION_PHRASE = "DELETE"

#: What Art. 12(3) GDPR gives us to answer in. Stated to the creator, and the clock the daily
#: operator report measures an open request against — one month, not "30 days": the statute counts
#: months, and no UI string of ours promises a duration in days.
DEADLINE = timedelta(days=31)


class DataRequestError(Exception):
    """A refusal, by code. Never a sentence: the words are the screen's (ADR 0021)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def available() -> bool:
    """Whether this machine offers the two requests at all (`CUSTOMER_DATA_REQUESTS_ENABLED`).

    Off by default and read where the request is answered, so the routes are one plain 404 and the
    screen says the step is not switched on here — never a half-offered button.
    """
    return bool(settings.CUSTOMER_DATA_REQUESTS_ENABLED)


def requests_for(organisation: Organisation) -> list[CustomerDataRequest]:
    """Every request this organisation ever made, newest first. Read-only."""
    return list(CustomerDataRequest.objects.filter(organisation=organisation))


def open_of(organisation: Organisation, kind: str) -> CustomerDataRequest | None:
    """This organisation's open request of that kind, or None. Scoped, so never anybody else's."""
    return (
        CustomerDataRequest.objects.filter(
            organisation=organisation, kind=kind, status__in=CustomerDataRequest.OPEN_STATUSES
        )
        .order_by("pk")
        .first()
    )


def open_request(
    organisation: Organisation,
    kind: str,
    *,
    by: User | None = None,
    now: datetime | None = None,
) -> CustomerDataRequest:
    """Record that this organisation asked for an export, or for its data to be deleted.

    Raises `unknown_kind` for anything but the two kinds, and `already_open` when one of that kind
    is already open — which is not a failure the creator has to do anything about, and the screen
    says so. Nothing is exported and nothing is deleted here (ADR 0045, ADR 0056).
    """
    if kind not in Kind.values:
        raise DataRequestError("unknown_kind")
    moment = now or timezone.now()
    try:
        with transaction.atomic():
            row = CustomerDataRequest.objects.create(
                organisation=organisation, kind=kind, requested_at=moment, requested_by=by
            )
    except IntegrityError as error:
        # The partial unique index, not a race we have to report: one open request per kind.
        raise DataRequestError("already_open") from error
    logger.info(
        "organisation %s asked for a %s of its data; answer due %s (Art. 12(3) GDPR)",
        organisation.pk,
        kind,
        (moment + DEADLINE).date().isoformat(),
    )
    return row


def withdraw(
    organisation: Organisation, kind: str, *, now: datetime | None = None
) -> CustomerDataRequest:
    """Take back this organisation's open request of that kind.

    `not_open` when there is none — which is also the answer for another organisation's request,
    because `open_of` never leaves this organisation. Only an open one can be withdrawn: a request
    already answered is the record of what happened and is never rewritten.
    """
    if kind not in Kind.values:
        raise DataRequestError("unknown_kind")
    row = open_of(organisation, kind)
    if row is None:
        raise DataRequestError("not_open")
    row.status = Status.WITHDRAWN
    row.settled_at = now or timezone.now()
    row.save(update_fields=("status", "settled_at"))
    logger.info("organisation %s withdrew its %s request", organisation.pk, kind)
    return row


def start(row: CustomerDataRequest, *, note: str = "") -> CustomerDataRequest:
    """A staff member picked the request up. Still nothing is exported or deleted."""
    if not row.open:
        raise DataRequestError("not_open")
    row.status = Status.IN_PROGRESS
    row.staff_note = note[:200] or row.staff_note
    row.save(update_fields=("status", "staff_note"))
    return row


def settle(
    row: CustomerDataRequest, *, note: str = "", now: datetime | None = None
) -> CustomerDataRequest:
    """The request was answered — by hand, out of band, by a staff member who says so here.

    What *answering* means is deliberately outside this module: for an export it is
    `manage.py export_customer_data` and a delivery the operator makes; for a deletion it is
    `manage.py end_pilot`, whose date is the human decision ADR 0045 insists on.
    """
    if not row.open:
        raise DataRequestError("not_open")
    row.status = Status.DONE
    row.settled_at = now or timezone.now()
    row.staff_note = note[:200] or row.staff_note
    row.save(update_fields=("status", "settled_at", "staff_note"))
    logger.info("%s request of organisation %s answered", row.kind, row.organisation_id)
    return row


@dataclass(frozen=True)
class Waiting:
    """Open requests, for the daily operator report. Counts and days — no organisation, no name.

    `crawler/safety_digest.py` renders it and may not import this module at all (import-linter:
    `crawler` never imports `marceline`), so the project package hands the value in through the
    hook `marceline/apps.py` installs. That is also why this is a plain frozen dataclass of two
    numbers: it crosses a module boundary and must carry nothing that could turn into content.
    """

    waiting: int
    oldest_days: int | None

    @property
    def any_waiting(self) -> bool:
        return self.waiting > 0


def waiting(now: datetime | None = None) -> Waiting:
    """How many requests are open, and how old the oldest one is in whole days.

    The number the daily report carries, so a request cannot sit unseen until the month is up. It
    counts every organisation's open requests together: which organisation asked belongs in the
    admin, not in a mail.
    """
    moment = now or timezone.now()
    oldest = (
        CustomerDataRequest.objects.filter(status__in=CustomerDataRequest.OPEN_STATUSES)
        .order_by("requested_at")
        .values_list("requested_at", flat=True)
        .first()
    )
    count = CustomerDataRequest.objects.filter(status__in=CustomerDataRequest.OPEN_STATUSES).count()
    return Waiting(
        waiting=count,
        oldest_days=None if oldest is None else max(0, (moment - oldest).days),
    )
