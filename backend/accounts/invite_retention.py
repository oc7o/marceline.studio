"""Deletion clock for a customer invite that never became an account (ADR 0052 amendment).

`accounts.CustomerInvite` holds personal data before any account exists: the invited email, a
staff note, the two declarations' verbatim text, the answered country and — until redemption — a
Django password hash. A **redeemed** invite is not this module's concern: the row that carries all
of that becomes an account (`organisation`, `redeemed_user`, `redeemed_at` all set together, in the
same transaction, by `accounts.invites.complete`) and from then on it is audit trail for a real
customer, not a standing pile of data about somebody with no account. This module is the clock for
the other case — expired, revoked or simply abandoned — which had none.

**"Never redeemed" is `redeemed_at IS NULL`, never "`organisation IS NULL`".** An invite that joins
an *existing* organisation (as opposed to creating a new one) has `organisation` set from the
moment it is issued (`accounts.invites.issue`) — that field names the destination, not the
outcome, and stays set whether or not the invite is ever redeemed. Only `redeemed_at` (and
`redeemed_user`, written in the same statement) tells you whether the redemption happened, so that
is the only field this module treats as the "redeemed" test.

**The clock starts when the invite stopped being answerable, not when it was issued**: the
revocation timestamp for a revoked one, `expires_at` for one that simply ran out. Both being set at
once is possible (a revoked invite whose `expires_at` has also passed); the revocation is the more
recent, deliberate fact, so it wins. A still-open invite — not revoked, not yet expired — is never
a candidate, whatever `retain_days` says, because neither timestamp exists yet to start a clock
from.

**A handed-over redemption is never touched here** (ADR 0052 §9). `handed_over_at` marks an invite
whose finished redemption is sitting in `delivery.OutboxItem`, waiting for development to apply it
through `accounts.invites.apply_redemption`; on development's own authoritative row that field is
always empty (a handover only ever happens against production's *local* copy of the row,
`marceline/accounts_replica.py` — development's `complete()` runs directly and never sets it), but
the filter excludes it anyway rather than resting on which host this happens to run on.

**What this changes on production: nothing extra.** `marceline/export.py::_open_invites` already
excludes a revoked or expired invite from the signed accounts document (its own filter is
`revoked_at__isnull=True, redeemed_at__isnull=True, expires_at__gt=now`), and
`marceline/accounts_replica.py::_apply_invites` already deletes production's copy of an invite that
has left that document, on the very next pull. By the time a row is a candidate here it has already
dropped out of what production reads; deleting it on development changes nothing about what
production holds.

`accounts.DocumentAcceptance.invite` is `on_delete=SET_NULL`: an accepted document's record
survives the invite's deletion, exactly as it survives an account closing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import CustomerInvite

#: The operator's number until this is confirmed against the pilot contract stack, like the other
#: retention periods (`CUSTOMER_DATA_RETENTION_DAYS`, `SEARCH_QUERY_RETENTION_DAYS`). Mirrored by
#: `settings.ONBOARDING_INVITE_RETENTION_DAYS`, which the command reads; this module stays pure and
#: does not import settings.
ONBOARDING_INVITE_RETENTION_DAYS = 30


@dataclass
class InviteRetentionReport:
    dry_run: bool
    now: datetime
    retain_days: int
    delete_before: datetime
    #: Unredeemed invites with a clock running at all (expired or revoked), any age.
    clocked: int = 0
    #: Due for deletion: past `delete_before`, excluding one in flight to the outbox.
    due: int = 0
    #: Due, but kept because its redemption is sitting in the outbox, not yet applied.
    in_flight: int = 0
    #: Ids actually deleted. Empty on a dry run.
    deleted_ids: list[int] = field(default_factory=list)


def apply_retention(
    *,
    now: datetime | None = None,
    dry_run: bool = True,
    retain_days: int = ONBOARDING_INVITE_RETENTION_DAYS,
) -> InviteRetentionReport:
    """Delete an unredeemed invite `retain_days` after it stopped being usable. Idempotent.

    Candidates are `redeemed_at IS NULL` (never became an account) and either revoked
    (`revoked_at` past the window) or expired (`expires_at` past it, only while not revoked — a
    revocation is the more recent fact and always wins). An invite whose redemption is still in
    the outbox (`handed_over_at IS NOT NULL`) is excluded outright and reported separately, never
    silently skipped.
    """
    now = now or timezone.now()
    delete_before = now - timedelta(days=retain_days)
    unredeemed = CustomerInvite.objects.filter(redeemed_at__isnull=True)

    clock_started = Q(revoked_at__isnull=False) | Q(revoked_at__isnull=True, expires_at__lte=now)
    clocked = unredeemed.filter(clock_started)

    stopped_before = Q(revoked_at__lte=delete_before) | Q(
        revoked_at__isnull=True, expires_at__lte=delete_before
    )
    due_all = unredeemed.filter(stopped_before)
    in_flight_ids = sorted(
        due_all.filter(handed_over_at__isnull=False).values_list("pk", flat=True)
    )
    due_ids = sorted(due_all.filter(handed_over_at__isnull=True).values_list("pk", flat=True))

    report = InviteRetentionReport(
        dry_run=dry_run,
        now=now,
        retain_days=retain_days,
        delete_before=delete_before,
        clocked=clocked.count(),
        due=len(due_ids),
        in_flight=len(in_flight_ids),
    )
    if dry_run or not due_ids:
        return report
    with transaction.atomic():
        CustomerInvite.objects.filter(pk__in=due_ids).delete()
    report.deleted_ids = due_ids
    return report
