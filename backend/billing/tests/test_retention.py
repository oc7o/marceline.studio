"""Invoice retention (ADR 0032 amendment 2026-09-17): eight years from the end of the issuing
year, enforced by `billing.retention` and by the database trigger it mirrors (migration 0004)."""

from collections.abc import Callable
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction

from accounts.models import Organisation
from billing.models import Invoice, InvoiceLine
from billing.retention import RETENTION_YEARS, apply_retention, deletable_from
from billing.services import cancel, issue

pytestmark = pytest.mark.usefixtures("seller")


def draft(organisation: Organisation) -> Invoice:
    invoice = Invoice.objects.create(organisation=organisation)
    InvoiceLine.objects.create(invoice=invoice, description="Service", unit_net_cents=10_000)
    return invoice


def _deadline(invoice: Invoice) -> date:
    assert invoice.issued_on is not None
    return deletable_from(invoice.issued_on)


def _number(invoice: Invoice) -> str:
    assert invoice.number is not None
    return invoice.number


# --- the rule itself -----------------------------------------------------------------------------


def test_retention_is_eight_years() -> None:
    assert RETENTION_YEARS == 8


def test_calendar_year_of_issue_is_what_counts_not_the_issue_date() -> None:
    # An invoice issued on the first day of a year and one issued on its last day become
    # deletable on the same day: the clock starts at the end of the calendar year, not at the
    # exact issue date (§ 147 Abs. 3 AO).
    assert deletable_from(date(2018, 1, 1)) == date(2027, 1, 1)
    assert deletable_from(date(2018, 12, 31)) == date(2027, 1, 1)


# --- boundary --------------------------------------------------------------------------------


def test_boundary_one_day_inside_and_one_day_outside(
    make_customer: Callable[..., Organisation],
) -> None:
    customer = make_customer()
    invoice = issue(draft(customer), on=date(2018, 5, 10))
    deadline = _deadline(invoice)
    assert deadline == date(2027, 1, 1)

    still_retained = apply_retention(deadline - date.resolution)
    assert still_retained.deleted == []
    assert Invoice.objects.filter(pk=invoice.pk).exists()

    now_deletable = apply_retention(deadline)
    assert now_deletable.deleted == [_number(invoice)]
    assert not Invoice.objects.filter(pk=invoice.pk).exists()


# --- what deletion takes with it --------------------------------------------------------------


def test_pdf_and_lines_go_with_the_row(make_customer: Callable[..., Organisation]) -> None:
    invoice = issue(draft(make_customer()), on=date(2018, 5, 10))
    assert invoice.pdf
    line_ids = list(invoice.lines.values_list("pk", flat=True))

    apply_retention(_deadline(invoice))

    assert not Invoice.objects.filter(pk=invoice.pk).exists()
    assert not InvoiceLine.objects.filter(pk__in=line_ids).exists()


# --- dry run ---------------------------------------------------------------------------------


def test_dry_run_writes_nothing(make_customer: Callable[..., Organisation]) -> None:
    invoice = issue(draft(make_customer()), on=date(2018, 5, 10))
    deadline = _deadline(invoice)

    result = apply_retention(deadline, dry_run=True)

    assert result.deleted == [_number(invoice)]
    assert Invoice.objects.filter(pk=invoice.pk).exists()
    refreshed = Invoice.objects.get(pk=invoice.pk)
    assert refreshed.pdf == invoice.pdf


def test_idempotent(make_customer: Callable[..., Organisation]) -> None:
    invoice = issue(draft(make_customer()), on=date(2018, 5, 10))
    deadline = _deadline(invoice)

    first = apply_retention(deadline)
    second = apply_retention(deadline)

    assert first.deleted == [_number(invoice)]
    assert second.deleted == []


# --- a cancellation protects its invoice, and is deleted with it -----------------------------


def test_a_cancellation_keeps_its_invoice_until_it_is_also_past_retention(
    make_customer: Callable[..., Organisation],
) -> None:
    customer = make_customer()
    invoice = issue(draft(customer), on=date(2018, 5, 10))
    storno = cancel(invoice, on=date(2019, 1, 15))  # later year: its own deadline is later

    invoice_deadline = _deadline(invoice)
    storno_deadline = _deadline(storno)
    assert storno_deadline > invoice_deadline

    kept = apply_retention(invoice_deadline)
    assert kept.deleted == []
    assert kept.kept_for_cancellation == [_number(invoice)]
    assert Invoice.objects.filter(pk__in=(invoice.pk, storno.pk)).count() == 2

    done = apply_retention(storno_deadline)
    assert sorted(done.deleted) == sorted([_number(invoice), _number(storno)])
    assert not Invoice.objects.filter(pk__in=(invoice.pk, storno.pk)).exists()


# --- the database enforces the same rule, for raw SQL too -------------------------------------


def test_the_database_refuses_delete_before_retention_and_allows_it_after(
    make_customer: Callable[..., Organisation],
) -> None:
    # The trigger checks CURRENT_DATE (nothing set the `apply_retention` override here), so it
    # is exercised directly with raw SQL rather than through that function: a recently issued
    # invoice (retained for years yet) and one issued so long ago that whatever day the test
    # happens to run, its retention has passed.
    customer = make_customer()
    recent = issue(draft(customer), on=date(2018, 5, 10))
    # Straight at `billing_invoice`, skipping the line first (unlike `Invoice.delete()`'s own
    # collector), so the invoice trigger's own message is what actually surfaces.
    with (
        pytest.raises(IntegrityError, match="retained until 2027-01-01"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM billing_invoice WHERE id = %s", [recent.pk])

    long_past = issue(draft(customer), on=date(2000, 1, 10))
    with connection.cursor() as cursor:
        # No DB-level ON DELETE CASCADE (Django's CASCADE is enforced in the ORM, not the
        # schema), so the line goes first, exactly as `Invoice.delete()` does it.
        cursor.execute("DELETE FROM billing_invoiceline WHERE invoice_id = %s", [long_past.pk])
        cursor.execute("DELETE FROM billing_invoice WHERE id = %s", [long_past.pk])
    assert not Invoice.objects.filter(pk=long_past.pk).exists()


# --- the command -----------------------------------------------------------------------------


def test_command_is_idempotent_and_dry_run_first(
    make_customer: Callable[..., Organisation],
) -> None:
    invoice = issue(draft(make_customer()), on=date(2018, 5, 10))
    deadline = _deadline(invoice)
    out = StringIO()

    call_command("invoice_retention", "--dry-run", "--date", deadline.isoformat(), stdout=out)
    assert f"would delete invoices ['{_number(invoice)}']" in out.getvalue()
    assert Invoice.objects.filter(pk=invoice.pk).exists()

    call_command("invoice_retention", "--date", deadline.isoformat(), stdout=out)
    call_command("invoice_retention", "--date", deadline.isoformat(), stdout=out)
    assert not Invoice.objects.filter(pk=invoice.pk).exists()
    assert "delete invoices none" in out.getvalue()
