"""Sequential, gap-free invoice numbers (GoBD, § 14 (4) Nr. 4 UStG).

One series per calendar year, shared by invoices and cancellations: `<prefix><year>-<sequence>`,
e.g. `2026-00001`. `next_number` must run inside the transaction that issues the document: it locks
the year's `InvoiceSequence` row, so the number is handed out only if that transaction commits
(no gaps), and a concurrent issue waits instead of taking the same number (no duplicates).
"""

from django.conf import settings
from django.db import transaction

from billing.models import InvoiceSequence

SEQUENCE_DIGITS = 5


def format_number(prefix: str, year: int, sequence: int) -> str:
    return f"{prefix}{year}-{sequence:0{SEQUENCE_DIGITS}d}"


def next_number(year: int) -> str:
    """Take the next number of `year`. Caller holds a transaction (asserted)."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("next_number must run inside the issuing transaction")
    # Insert the year's row if it is the first document of the year; a concurrent first insert
    # simply loses the race and both then lock the same row.
    InvoiceSequence.objects.bulk_create([InvoiceSequence(year=year)], ignore_conflicts=True)
    sequence = InvoiceSequence.objects.select_for_update().get(year=year)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number"])
    return format_number(str(settings.BILLING_INVOICE_NUMBER_PREFIX), year, sequence.last_number)
