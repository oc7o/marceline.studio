"""The bound attestation: what development checks before the relay may act (ADR 0044 §9, amendment
2026-09-20).

The public host authenticates the customer's session and checks CSRF, then *names* the acting
organisation and user in two headers (`marceline/uploads/relay.py`). Development cannot verify that
a session really existed — the browser only ever talks to production, so any proof production could
present, production could also have minted for a customer of its choosing. That is why there is no
signed upload grant here: it would be theatre (the ADR amendment says so in as many words).

What development *can* do is refuse every pair that is not a real, entitled customer, bound how far
one credential may spread, and count what it refused. Three checks, all on this machine:

1. **The pair is ours and may act.** Uploads are available here at all (the same gate
   `app/uploads._customer` applies to a browser), the two headers parse, a `Membership` links
   exactly that user to exactly that organisation, and the account is active — a switched-off
   account's own session stops working, and the relay must not outlive it.
2. **One credential may not quietly serve the whole customer base.** Starting an upload for an
   organisation that has not used the relay in `WINDOW` is refused once `MAX_ORGANISATIONS` others
   already have. A private beta never reaches it; a `web` walking every customer does, on the
   26th.
3. **Every refusal is counted** (`marceline/uploads/models.py`), so the difference between "nobody
   is using this" and "something is probing it" is a query rather than a guess.

**Every refusal of the first check is the same `404 not_found`** — byte for byte the answer an
unknown upload gets. Which of the four it was is exactly what must not be learnable from outside:
the relay's caller would otherwise have an oracle for which organisation ids and user ids exist.
The reason is written to this machine's own table instead.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Final, NoReturn

from django.contrib.auth.models import User
from django.db import DatabaseError
from django.db.models import F
from django.utils import timezone

from accounts.models import Membership, Organisation
from marceline.app import uploads
from marceline.models import MediaUpload
from marceline.uploads.models import RelayRefusal

logger = logging.getLogger("marceline.uploads.attestation")

Reason = RelayRefusal.Reason

#: How far back the spread ceiling looks.
WINDOW: Final = timedelta(hours=24)
#: How many distinct organisations one relay credential may start an upload for inside `WINDOW`.
#: Product policy, not a technical limit: the private beta is a handful of creators, and a number
#: this far above it only ever refuses something nobody planned. Raise it deliberately, here, when
#: the beta outgrows it — a refused customer is a 503 with a code, and a `spread` row says why.
MAX_ORGANISATIONS: Final = 25


def record(reason: str, *, now: datetime | None = None) -> None:
    """Count one refusal in its hour's row. Never raises: an audit that can fail a request would
    be a way to turn this table into a denial of service."""
    moment = now or timezone.now()
    hour = moment.replace(minute=0, second=0, microsecond=0)
    try:
        row, created = RelayRefusal.objects.get_or_create(
            hour=hour,
            reason=reason,
            defaults={"attempts": 1, "first_at": moment, "last_at": moment},
        )
        if not created:
            RelayRefusal.objects.filter(pk=row.pk).update(
                attempts=F("attempts") + 1, last_at=moment
            )
    except DatabaseError:
        logger.error("relay attestation: refusal %s was not recorded", reason)


def _refuse(reason: str) -> NoReturn:
    """One answer for every reason, and the reason kept here rather than sent."""
    record(reason)
    logger.warning("relay attestation refused: %s", reason)
    raise uploads.Refused(404, "not_found")


def actor(organisation_header: str | None, user_header: str | None) -> tuple[User, Organisation]:
    """The customer the public host says is acting, verified against development's own rows.

    Raises `uploads.Refused(404, "not_found")` for every failure alike.
    """
    if not uploads.uploads_available():
        _refuse(Reason.UNAVAILABLE)
    try:
        organisation_id = int(organisation_header or "")
        user_id = int(user_header or "")
    except (TypeError, ValueError):
        _refuse(Reason.MALFORMED)
    membership = (
        Membership.objects.select_related("organisation", "user")
        .filter(user_id=user_id, organisation_id=organisation_id)
        .first()
    )
    if membership is None:
        _refuse(Reason.NO_MEMBERSHIP)
    if not membership.user.is_active:
        _refuse(Reason.INACTIVE)
    return membership.user, membership.organisation


def organisations_in_window(*, now: datetime | None = None) -> set[int]:
    """The organisations the relay has started an upload for inside `WINDOW`."""
    moment = now or timezone.now()
    return set(
        MediaUpload.objects.filter(origin=MediaUpload.Origin.RELAY, created_at__gte=moment - WINDOW)
        .values_list("organisation_id", flat=True)
        .distinct()
    )


def admit(organisation: Organisation, *, now: datetime | None = None) -> None:
    """Let the relay start an upload for this organisation, or refuse the whole request.

    Applied to the declare step only: it is the one route that admits an organisation the relay was
    not already serving. A chunk, a completion or a cancel belongs to a row this credential created,
    which is narrower still, and a read cannot widen anything.

    The refusal is `503 uploads_unavailable` — the code a customer already sees when this machine is
    not taking files, and no oracle about anybody: the pair has resolved by the time it is asked.
    """
    moment = now or timezone.now()
    serving = organisations_in_window(now=moment)
    if organisation.pk in serving or len(serving) < MAX_ORGANISATIONS:
        return
    record(Reason.SPREAD, now=moment)
    logger.warning("relay attestation: more than %s organisations in %s", MAX_ORGANISATIONS, WINDOW)
    raise uploads.Refused(503, "uploads_unavailable")
