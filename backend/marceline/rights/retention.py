"""Deleting closed rights records once they are no longer needed (ADR 0034).

Baseline: a **closed** record is deleted after the end of the third calendar year following its
last relevant event — an event in 2026 keeps the record through 31 December 2029 and makes it
deletable from 1 January 2030 — unless a legal hold is set on it or on one of its proofs. The
period follows the German limitation framework (§§ 195, 199 BGB via § 102 UrhG); it is a product
policy, not a statutory retention duty.

What is closed:
- a work record in `revoked`, `expired` or `superseded`; its last event is its latest transition.
  It goes together with its transitions, its proofs (vault file first, then the row) and their
  access log;
- a mandate that is revoked or past its end date and that no remaining record points at; its last
  event is the revocation or the end date. It goes with its proofs and events.

Open records, disputed ones included, are never deleted here. Organisation-level data (claimant,
partners, unlinked proofs, events) stay while the account exists; account closure is a follow-up.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from django.db import transaction
from django.db.models import Max

from marceline.rights import vault
from marceline.rights.models import (
    CLOSED_STATUSES,
    AgencyMandate,
    ProofDocument,
    RightsEvent,
    WorkRights,
)


def deletable_from(last_event: date | datetime) -> date:
    """The first day a record whose last relevant event was `last_event` may be deleted."""
    return date(last_event.year + 4, 1, 1)


@dataclass
class RetentionResult:
    works: list[int] = field(default_factory=list)
    mandates: list[int] = field(default_factory=list)
    proofs: list[int] = field(default_factory=list)
    held: list[str] = field(default_factory=list)


def _delete_proofs(proofs: list[ProofDocument], result: RetentionResult, dry_run: bool) -> None:
    for proof in proofs:
        result.proofs.append(proof.pk)
        if not dry_run:
            vault.delete(proof.storage_name)
            proof.delete()  # its access log goes with it (RightsEvent.proof CASCADE)


def apply_retention(today: date, *, dry_run: bool = False) -> RetentionResult:
    """Delete every closed record past its retention date. Idempotent; `dry_run` only reports."""
    result = RetentionResult()
    works = (
        WorkRights.objects.filter(status__in=CLOSED_STATUSES)
        .annotate(last_event=Max("transitions__at"))
        .order_by("pk")
    )
    for work in works:
        last = work.last_event or work.status_changed_at
        if deletable_from(last) > today:
            continue
        proofs = list(work.proofs.all())
        if work.legal_hold or any(proof.legal_hold for proof in proofs):
            result.held.append(f"work {work.pk}")
            continue
        with transaction.atomic():
            _delete_proofs(proofs, result, dry_run)
            result.works.append(work.pk)
            if not dry_run:
                work.delete()  # transitions CASCADE

    deleted_works = set(result.works)
    for mandate in AgencyMandate.objects.order_by("pk"):
        ends = [moment for moment in (mandate.revoked_at, mandate.valid_until) if moment]
        if not ends or (
            mandate.revoked_at is None and mandate.valid_until and mandate.valid_until >= today
        ):
            continue
        last = mandate.revoked_at or mandate.valid_until
        assert last is not None
        if deletable_from(last) > today:
            continue
        if mandate.works.exclude(pk__in=deleted_works).exists():
            continue
        proofs = list(mandate.proofs.all())
        if mandate.legal_hold or any(proof.legal_hold for proof in proofs):
            result.held.append(f"mandate {mandate.pk}")
            continue
        with transaction.atomic():
            _delete_proofs(proofs, result, dry_run)
            result.mandates.append(mandate.pk)
            if not dry_run:
                RightsEvent.objects.filter(mandate=mandate).delete()
                mandate.delete()
    return result
