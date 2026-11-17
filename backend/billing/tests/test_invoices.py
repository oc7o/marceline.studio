"""Numbering, immutability, cancellation, payment state and the subscription run — against Postgres.

The GoBD rules are asserted twice: through the services, and with raw writes that bypass them, so
the database triggers are shown to hold on their own.
"""

import hashlib
import re
from collections.abc import Callable
from datetime import date
from importlib import import_module
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from billing import services
from billing.models import Invoice, InvoiceLine, InvoiceSequence, Plan, Subscription
from billing.services import BillingError, cancel, issue, issue_due_invoices, mark_paid, status_of

ON = date(2026, 9, 17)

pytestmark = pytest.mark.usefixtures("seller")


def draft(organisation: Organisation, *lines: tuple[str, int, int]) -> Invoice:
    invoice = Invoice.objects.create(organisation=organisation)
    for position, (description, quantity, unit) in enumerate(lines or (("Service", 1, 10_000),), 1):
        InvoiceLine.objects.create(
            invoice=invoice,
            position=position,
            description=description,
            quantity=quantity,
            unit_net_cents=unit,
        )
    return invoice


# --- numbering ---------------------------------------------------------------------------------


def test_numbers_are_sequential_per_year(make_customer: Callable[..., Organisation]) -> None:
    customer = make_customer()

    first = issue(draft(customer), on=ON)
    second = issue(draft(customer), on=ON)
    next_year = issue(draft(customer), on=date(2027, 1, 2))

    assert (first.number, second.number, next_year.number) == (
        "2026-00001",
        "2026-00002",
        "2027-00001",
    )


def test_prefix_comes_from_settings(
    make_customer: Callable[..., Organisation], settings: Settings
) -> None:
    settings.BILLING_INVOICE_NUMBER_PREFIX = "RE-"
    assert issue(draft(make_customer()), on=ON).number == "RE-2026-00001"


def test_a_failed_issue_leaves_no_gap(make_customer: Callable[..., Organisation]) -> None:
    customer = make_customer()
    abroad = make_customer("Consumer", country="FR", is_business=False)

    issue(draft(customer), on=ON)
    with pytest.raises(BillingError, match="consumer_outside_germany"):
        issue(draft(abroad), on=ON)
    with pytest.raises(BillingError, match="at least one line"):
        issue(Invoice.objects.create(organisation=customer), on=ON)

    assert issue(draft(customer), on=ON).number == "2026-00002"


def test_a_rolled_back_transaction_gives_its_number_back(
    make_customer: Callable[..., Organisation],
) -> None:
    customer = make_customer()
    with pytest.raises(RuntimeError), transaction.atomic():
        issue(draft(customer), on=ON)
        raise RuntimeError("the caller's transaction fails after issuing")

    assert issue(draft(customer), on=ON).number == "2026-00001"


def test_cancellations_share_the_series(make_customer: Callable[..., Organisation]) -> None:
    invoice = issue(draft(make_customer()), on=ON)
    storno = cancel(invoice, on=ON)
    assert storno.number == "2026-00002"


def test_a_sequence_never_goes_down_or_away(make_customer: Callable[..., Organisation]) -> None:
    issue(draft(make_customer()), on=ON)
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceSequence.objects.filter(year=2026).update(last_number=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceSequence.objects.filter(year=2026).delete()


def test_numbering_requires_the_issuing_transaction() -> None:
    from billing.numbering import next_number

    with pytest.raises(RuntimeError):
        next_number(2026)


# --- issuing -----------------------------------------------------------------------------------


def test_issue_snapshots_totals_seller_buyer_and_the_pdf(
    make_customer: Callable[..., Organisation],
) -> None:
    invoice = issue(draft(make_customer(), ("A", 2, 4_950), ("B", 1, 101)), on=ON)

    assert (invoice.net_cents, invoice.vat_cents, invoice.gross_cents) == (10_001, 1_900, 11_901)
    assert (invoice.vat_treatment, invoice.vat_rate_bp) == ("domestic", 1900)
    assert invoice.due_on == date(2026, 10, 1)
    assert invoice.seller is not None and invoice.seller["name"] == "Example Seller"
    assert invoice.seller["iban"] == "DE89370400440532013000"
    assert invoice.buyer == {
        "name": "Studio GmbH",
        "address": ["Example Road 2", "54321 Example Town"],
        "country": "DE",
        "is_business": True,
        "vat_id": "",
    }
    stored = Invoice.objects.get(pk=invoice.pk)
    assert bytes(stored.pdf or b"").startswith(b"%PDF-")
    assert stored.pdf_sha256 == hashlib.sha256(bytes(stored.pdf or b"")).hexdigest()


def test_reverse_charge_for_an_eu_business(make_customer: Callable[..., Organisation]) -> None:
    customer = make_customer("Wien", country="AT", vat_id="atu 12345678")
    invoice = issue(draft(customer), on=ON)
    assert (invoice.vat_treatment, invoice.vat_cents, invoice.gross_cents) == (
        "reverse_charge",
        0,
        10_000,
    )
    assert invoice.buyer is not None and invoice.buyer["vat_id"] == "ATU12345678"


def test_small_business_setting(
    make_customer: Callable[..., Organisation], settings: Settings
) -> None:
    settings.BILLING_SMALL_BUSINESS = "true"
    invoice = issue(draft(make_customer()), on=ON)
    assert (invoice.vat_treatment, invoice.vat_cents) == ("small_business", 0)


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("BILLING_SELLER_NAME", "", "BILLING_SELLER_NAME missing"),
        ("BILLING_SELLER_IBAN", "DE00 0000", "BILLING_SELLER_IBAN invalid"),
        ("BILLING_SMALL_BUSINESS", "", "BILLING_SMALL_BUSINESS not decided"),
    ],
)
def test_issuing_refuses_an_incomplete_seller(
    make_customer: Callable[..., Organisation],
    settings: Settings,
    setting: str,
    value: str,
    message: str,
) -> None:
    setattr(settings, setting, value)
    with pytest.raises(BillingError, match=message):
        issue(draft(make_customer()), on=ON)
    assert not InvoiceSequence.objects.exists()


def test_a_customer_without_billing_profile_is_refused(db: None) -> None:
    organisation = Organisation.objects.create(name="No profile")
    with pytest.raises(BillingError, match="no billing profile"):
        issue(draft(organisation), on=ON)


def test_an_issued_invoice_is_not_issued_twice(make_customer: Callable[..., Organisation]) -> None:
    invoice = issue(draft(make_customer()), on=ON)
    with pytest.raises(BillingError, match="already issued"):
        issue(invoice, on=ON)


# --- immutability ------------------------------------------------------------------------------


def test_an_issued_invoice_cannot_be_changed(make_customer: Callable[..., Organisation]) -> None:
    invoice = issue(draft(make_customer()), on=ON)

    attempts: list[dict[str, Any]] = [
        {"net_cents": 1, "gross_cents": 1901},
        {"number": "2026-00099"},
        {"buyer": {}},
    ]
    for changes in attempts:
        with pytest.raises(IntegrityError, match="immutable"), transaction.atomic():
            Invoice.objects.filter(pk=invoice.pk).update(**changes)
    with pytest.raises(IntegrityError), transaction.atomic():
        invoice.delete()


def test_saving_an_unchanged_issued_invoice_is_allowed(
    make_customer: Callable[..., Organisation],
) -> None:
    invoice = issue(draft(make_customer()), on=ON)
    Invoice.objects.get(pk=invoice.pk).save()


def test_lines_of_an_issued_invoice_are_frozen(make_customer: Callable[..., Organisation]) -> None:
    invoice = issue(draft(make_customer()), on=ON)
    line = invoice.lines.get()

    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceLine.objects.filter(pk=line.pk).update(unit_net_cents=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        line.delete()
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceLine.objects.create(invoice=invoice, position=2, description="x", unit_net_cents=1)
    other = draft(invoice.organisation)
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceLine.objects.filter(invoice=other).update(invoice=invoice)


def test_a_draft_stays_editable_and_deletable(make_customer: Callable[..., Organisation]) -> None:
    invoice = draft(make_customer())
    InvoiceLine.objects.filter(invoice=invoice).update(unit_net_cents=5)
    invoice.delete()
    assert not Invoice.objects.exists()


def test_the_database_refuses_a_half_issued_row(
    make_customer: Callable[..., Organisation],
) -> None:
    invoice = draft(make_customer())
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.filter(pk=invoice.pk).update(number="2026-00001")


def test_triggers_hold_for_raw_sql(make_customer: Callable[..., Organisation]) -> None:
    invoice = issue(draft(make_customer()), on=ON)
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE billing_invoice SET due_on = NULL WHERE id = %s", [invoice.pk])


# --- public ids (ADR 0033) ---------------------------------------------------------------------


def test_an_issued_invoices_public_id_is_frozen_with_the_rest(
    make_customer: Callable[..., Organisation],
) -> None:
    invoice = issue(draft(make_customer()), on=ON)

    assert re.fullmatch(r"inv_[0-9a-f]{32}", invoice.public_id)
    with pytest.raises(IntegrityError, match="immutable"), transaction.atomic():
        Invoice.objects.filter(pk=invoice.pk).update(public_id="inv_" + "0" * 32)
    assert Invoice.objects.get(pk=invoice.pk).public_id == invoice.public_id


def test_adding_the_public_id_column_passes_the_immutability_trigger(
    make_customer: Callable[..., Organisation],
) -> None:
    """What `billing/0003` does to live data: issued invoices get ids, no trigger objects.

    The migration's own field and check run against a table that already holds issued documents
    (an invoice, its cancellation) and a draft — the rows a naive `UPDATE` backfill would fail on.
    """
    customer = make_customer()
    issued = issue(draft(customer), on=ON)
    cancellation = cancel(issued, on=ON)
    unissued = draft(customer)
    before = {row["id"]: row for row in Invoice.objects.values("id", "number", "pdf_sha256")}
    migration = import_module("billing.migrations.0003_invoice_public_id")
    [add_field, check] = migration.Migration.operations
    field = Invoice._meta.get_field("public_id")
    with connection.cursor() as cursor:
        # The test transaction holds deferred foreign-key checks; ALTER TABLE refuses to run then.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with connection.schema_editor() as editor:
        editor.remove_field(Invoice, field)
        new_field = add_field.field.clone()
        new_field.set_attributes_from_name("public_id")
        new_field.model = Invoice
        editor.add_field(Invoice, new_field)
    with connection.cursor() as cursor:
        cursor.execute(check.sql)

    ids = dict(Invoice.objects.values_list("pk", "public_id"))
    assert sorted(ids) == sorted([issued.pk, cancellation.pk, unissued.pk])
    assert len(set(ids.values())) == 3
    assert all(re.fullmatch(r"inv_[0-9a-f]{32}", value) for value in ids.values())
    assert ids[issued.pk] != issued.public_id, "a fresh id per row, written by the rewrite"
    assert {
        row["id"]: row for row in Invoice.objects.values("id", "number", "pdf_sha256")
    } == before
    # The trigger survived the rewrite and now guards the new column too.
    with pytest.raises(IntegrityError, match="immutable"), transaction.atomic():
        Invoice.objects.filter(pk=issued.pk).update(public_id="inv_" + "0" * 32)


# --- payment and status ------------------------------------------------------------------------


def test_payment_fields_stay_writable_and_drive_the_status(
    make_customer: Callable[..., Organisation],
) -> None:
    invoice = issue(draft(make_customer()), on=ON)

    assert status_of(invoice, ON) == "open"
    assert status_of(invoice, date(2026, 10, 1)) == "open"
    assert status_of(invoice, date(2026, 10, 2)) == "overdue"

    paid = mark_paid(invoice, paid_on=date(2026, 10, 5), note="bank statement 42")
    assert status_of(paid, date(2026, 12, 1)) == "paid"
    assert services.mark_unpaid(paid).paid_on is None


def test_a_draft_cannot_be_marked_paid(make_customer: Callable[..., Organisation]) -> None:
    with pytest.raises(BillingError):
        mark_paid(draft(make_customer()), paid_on=ON)
    assert status_of(draft(make_customer("Other")), ON) == "draft"


# --- cancellation ------------------------------------------------------------------------------


def test_cancellation_mirrors_the_invoice_and_leaves_it_untouched(
    make_customer: Callable[..., Organisation], settings: Settings
) -> None:
    invoice = issue(draft(make_customer(), ("A", 3, 3_333)), on=ON)
    before = Invoice.objects.filter(pk=invoice.pk).values().get()
    settings.BILLING_SMALL_BUSINESS = "true"  # a later settings change must not leak into it

    storno = cancel(invoice, on=date(2026, 9, 20))

    assert storno.kind == "cancellation" and storno.cancels_id == invoice.pk
    assert (storno.net_cents, storno.vat_cents, storno.gross_cents) == (-9_999, -1_900, -11_899)
    assert (storno.vat_treatment, storno.vat_rate_bp) == ("domestic", 1900)
    assert storno.buyer == invoice.buyer
    assert [(line.quantity, line.unit_net_cents) for line in storno.lines.all()] == [(3, -3_333)]
    assert Invoice.objects.filter(pk=invoice.pk).values().get() == before
    refreshed = Invoice.objects.select_related("cancellation").get(pk=invoice.pk)
    assert status_of(refreshed, ON) == "cancelled"
    assert status_of(storno, ON) == "issued"


def test_cancelling_twice_drafts_and_cancellations_is_refused(
    make_customer: Callable[..., Organisation],
) -> None:
    customer = make_customer()
    invoice = issue(draft(customer), on=ON)
    storno = cancel(invoice, on=ON)

    with pytest.raises(BillingError, match="already cancelled"):
        cancel(invoice, on=ON)
    with pytest.raises(BillingError, match="cannot be cancelled"):
        cancel(storno, on=ON)
    with pytest.raises(BillingError, match="draft"):
        cancel(draft(customer), on=ON)
    with pytest.raises(BillingError):
        mark_paid(storno, paid_on=ON)


# --- subscriptions -----------------------------------------------------------------------------


def test_the_run_bills_every_started_period_once(
    make_customer: Callable[..., Organisation], monthly_plan: Plan
) -> None:
    customer = make_customer()
    subscription = Subscription.objects.create(
        organisation=customer, plan=monthly_plan, starts_on=date(2026, 7, 31)
    )

    first = issue_due_invoices(on=date(2026, 8, 31))
    second = issue_due_invoices(on=date(2026, 8, 31))
    third = issue_due_invoices(on=date(2026, 9, 30))

    assert first.issued == ["2026-00001", "2026-00002"]
    assert second.issued == [] and second.due == []
    assert third.issued == ["2026-00003"]
    periods = list(
        subscription.invoices.order_by("period_start").values_list("period_start", "period_end")
    )
    assert periods == [
        (date(2026, 7, 31), date(2026, 8, 30)),
        (date(2026, 8, 31), date(2026, 9, 29)),
        (date(2026, 9, 30), date(2026, 10, 30)),
    ]
    line = Invoice.objects.get(number="2026-00001").lines.get()
    assert (line.description, line.unit_net_cents) == ("Marceline Professional (monthly)", 10_000)


def test_a_cancelled_period_is_not_billed_again(
    make_customer: Callable[..., Organisation], monthly_plan: Plan
) -> None:
    Subscription.objects.create(
        organisation=make_customer(), plan=monthly_plan, starts_on=date(2026, 9, 1)
    )
    [number] = issue_due_invoices(on=ON).issued
    cancel(Invoice.objects.get(number=number), on=ON)

    assert issue_due_invoices(on=ON).issued == []


def test_dry_run_writes_nothing(
    make_customer: Callable[..., Organisation], monthly_plan: Plan
) -> None:
    Subscription.objects.create(
        organisation=make_customer(), plan=monthly_plan, starts_on=date(2026, 9, 1)
    )
    run = issue_due_invoices(on=ON, dry_run=True)
    assert len(run.due) == 1 and run.issued == []
    assert not Invoice.objects.exists()


def test_one_failing_subscription_does_not_stop_the_others(
    make_customer: Callable[..., Organisation], monthly_plan: Plan
) -> None:
    abroad = make_customer("Abroad", country="US")
    home = make_customer("Home")
    for organisation in (abroad, home):
        Subscription.objects.create(
            organisation=organisation, plan=monthly_plan, starts_on=date(2026, 9, 1)
        )

    run = issue_due_invoices(on=ON)

    assert run.issued == ["2026-00001"]
    assert [reason for _, reason in run.failed] == ["VAT case not supported: outside_eu"]
    assert not Invoice.objects.filter(organisation=abroad).exists()


def test_the_run_refuses_without_seller_settings(
    make_customer: Callable[..., Organisation], monthly_plan: Plan, settings: Settings
) -> None:
    settings.BILLING_SELLER_IBAN = ""
    Subscription.objects.create(
        organisation=make_customer(), plan=monthly_plan, starts_on=date(2026, 9, 1)
    )
    with pytest.raises(BillingError, match="IBAN"):
        issue_due_invoices(on=ON)


def test_one_open_subscription_per_organisation(
    make_customer: Callable[..., Organisation], monthly_plan: Plan
) -> None:
    customer = make_customer()
    Subscription.objects.create(organisation=customer, plan=monthly_plan, starts_on=ON)
    with pytest.raises(IntegrityError), transaction.atomic():
        Subscription.objects.create(organisation=customer, plan=monthly_plan, starts_on=ON)
