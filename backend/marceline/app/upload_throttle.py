"""Per-organisation rate limit for customer writes to the fingerprint and rights APIs (issue #29,
ADR 0034), and for the customer's own data export (ADR 0045, issue #85).

A signed-in customer can script their own session (ADR 0028), so the browser is not what limits
how often `POST /api/v1/app/fingerprints` (or a rights write) runs. Each registration validates up
to tens of thousands of hash rows and queries them for duplicate claims; an organisation gets
`LIMITS[kind]` of those per rolling hour, counted in Postgres (`marceline.UploadAttempt`) so the
limit holds across gunicorn workers and restarts, like the login throttle (ADR 0023). Rights writes
are cheap by comparison but share the mechanism and gate the same production outbox path
(`marceline/app/rights_status.py`). `GET /api/v1/app/export` (`marceline/app/export.py`) is a read,
not a write, but it renders every customer document, every reference frame hash and every notice
draft in one call — as heavy as an upload — so it counts here too.

Admission and counting are one step under a transaction-level advisory lock per organisation, so
a parallel burst cannot slip past the limit. Every admitted attempt counts — a malformed upload
costs as much validation as a good one — and a refused attempt counts nothing.

Hashing a video in the browser takes minutes, so a real customer never comes near the limit; it
exists for a runaway script.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil

from django.db import connection, transaction
from django.utils import timezone

from marceline.models import UploadAttempt

Kind = UploadAttempt.Kind

WINDOW = timedelta(hours=1)
LIMITS: dict[str, int] = {
    Kind.ORIGINAL: 30,
    Kind.CLIENT_EVENT: 60,
    Kind.RIGHTS_WRITE: 60,
    # Notice writes (ADR 0046) are cheap on production — one outbox row — but each one becomes a
    # write development applies against the notice gate, so they share the mechanism.
    Kind.NOTICE_WRITE: 60,
    # One export per rolling hour: a real customer downloads their data occasionally, and a second
    # request while the first is still fresh gains them nothing — it is the same document.
    Kind.EXPORT: 1,
}
#: First key of the two-integer advisory lock (the login throttle uses 0x10614).
ADVISORY_LOCK_NAMESPACE = 0x10615


@dataclass(frozen=True)
class Refusal:
    retry_after_s: int


def admit(organisation_id: int, kind: str, *, now: datetime | None = None) -> Refusal | None:
    """Count this attempt, or refuse it with the seconds until the oldest one leaves the window."""
    moment = now if now is not None else timezone.now()
    limit = LIMITS[kind]
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                [ADVISORY_LOCK_NAMESPACE, organisation_id % 2**31],
            )
        start = moment - WINDOW
        UploadAttempt.objects.filter(organisation_id=organisation_id, created_at__lt=start).delete()
        recent = list(
            UploadAttempt.objects.filter(
                organisation_id=organisation_id, kind=kind, created_at__gte=start
            )
            .order_by("created_at")
            .values_list("created_at", flat=True)[:limit]
        )
        if len(recent) >= limit:
            wait = (recent[0] + WINDOW - moment).total_seconds()
            return Refusal(retry_after_s=max(1, ceil(wait)))
        UploadAttempt.objects.create(organisation_id=organisation_id, kind=kind, created_at=moment)
    return None
