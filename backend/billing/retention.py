"""Deleting invoices and their stored PDFs once the statutory retention period has passed.

Issued invoices and cancellations (Storno) are *Buchungsbelege* (accounting vouchers). Since
**1 January 2025** (Viertes Bürokratieentlastungsgesetz) they are kept **eight** years under
§ 147 Abs. 3 AO, § 257 Abs. 4 HGB and § 14b Abs. 1 UStG — not the ten years ADR 0032 used to say
(amendment 2026-09-17; ten years remains only for Handelsbücher, Inventare, Bilanzen and
Jahresabschlüsse, none of which live in this table). The period is counted from the **end of the
calendar year of issue**, not the issue date itself: an invoice issued on any day of year Y is
kept through 31 December of year Y + 8 and becomes deletable on 1 January of year Y + 9.

A draft (`number` is NULL) carries no statutory duty and is never touched here — it is deleted or
edited like any other row, through the admin or `billing.services`.

Deleting an invoice takes its stored PDF with it: `Invoice.pdf` is a column on the row, not a
separate file, so there is nowhere else for it to live. `InvoiceLine` rows go the same way
(`on_delete=CASCADE`).

A cancellation (Storno) is always issued on or after the invoice it cancels
(`billing.services.cancel`), so its own retention date never comes earlier. `Invoice.cancels`
(`on_delete=PROTECT`) keeps an original invoice from being deleted while its cancellation is
still retained; this module skips such an invoice itself, so the report says why rather than
raising `ProtectedError`, and deletes the cancellation first in the same run once it, too, is
past retention.

Billing has no legal-hold concept today, unlike `marceline.rights` (ADR 0034) — nothing here
checks one. If invoices ever grow one, gate the deletion here and in the database trigger
(`billing/migrations/0004_invoice_retention_deletion.py`) alongside it.

The database trigger that guards a DELETE (migration 0004) checks a real calendar date, not
whatever `today` this function was called with, so each deletion sets that date as a
transaction-local Postgres setting (`marceline.retention_today`) first — this is what keeps an
explicit `--date` (rehearsing or catching up, the same escape hatch `issue_invoices --date` and
`rights_housekeeping --date` already have) consistent with what actually gets deleted. Every
other path (a raw SQL DELETE, the admin) never sets it, so the trigger falls back to the real
clock there.
"""

from dataclasses import dataclass, field
from datetime import date

from django.db import connection, transaction

from billing.models import Invoice

# Since 1 January 2025 (Viertes Bürokratieentlastungsgesetz): § 147 Abs. 3 AO, § 257 Abs. 4 HGB,
# § 14b Abs. 1 UStG. Mirrored in SQL by billing_invoice_deletable_from (migration 0004) — change
# both together.
RETENTION_YEARS = 8


def deletable_from(issued_on: date) -> date:
    """The first day an invoice issued on `issued_on` may be deleted."""
    return date(issued_on.year + RETENTION_YEARS + 1, 1, 1)


@dataclass
class RetentionResult:
    deleted: list[str] = field(default_factory=list)
    kept_for_cancellation: list[str] = field(default_factory=list)


def _label(invoice: Invoice) -> str:
    return invoice.number or f"draft {invoice.pk}"


def apply_retention(today: date, *, dry_run: bool = False) -> RetentionResult:
    """Delete every issued invoice/cancellation past its retention date. Idempotent.

    Ordered newest-issued first: a cancellation is always issued on or after the invoice it
    protects, so this deletes a cancellation before the invoice it protects is even
    reconsidered, in the same run — the `Invoice.cancels` `PROTECT` never has a chance to fire
    for a row this same call is about to delete anyway.
    """
    result = RetentionResult()
    candidates = (
        Invoice.objects.filter(issued_on__isnull=False, issued_on__lt=today)
        .select_related("cancellation")
        .order_by("-issued_on", "-pk")
    )
    for invoice in candidates:
        assert invoice.issued_on is not None
        if deletable_from(invoice.issued_on) > today:
            continue
        cancellation = getattr(invoice, "cancellation", None)
        if cancellation is not None and (
            cancellation.issued_on is None or deletable_from(cancellation.issued_on) > today
        ):
            result.kept_for_cancellation.append(_label(invoice))
            continue
        result.deleted.append(_label(invoice))
        if not dry_run:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('marceline.retention_today', %s, true)",
                        [today.isoformat()],
                    )
                invoice.delete()  # lines CASCADE; the PDF lives only on this row
    return result
