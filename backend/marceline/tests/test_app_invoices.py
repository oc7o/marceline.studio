"""The customer's invoices (ADR 0032): scoped to the organisation, 404 for everything else.

A draft, another organisation's invoice and an id that does not exist must be indistinguishable —
same status, byte-identical body — for the list and the PDF alike.
"""

from datetime import date

import pytest
from django.contrib.auth.models import User
from django.test import Client
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from billing.models import BillingProfile, Invoice, InvoiceLine
from billing.services import cancel, issue, mark_paid
from billing.tests.helpers import SELLER
from core import public_ids
from marceline.tests.app_helpers import APP, add_member, client_for, make_organisation

#: A well-formed invoice id nobody has (ADR 0033).
UNKNOWN_INVOICE = public_ids.generate("inv")


@pytest.fixture(autouse=True)
def seller(settings: Settings) -> None:
    for name, value in SELLER.items():
        setattr(settings, name, value)


def customer(name: str) -> Organisation:
    organisation = make_organisation(name)
    BillingProfile.objects.create(
        organisation=organisation, legal_name=name, address="Road 1\n12345 Town", country="DE"
    )
    return organisation


def draft(organisation: Organisation) -> Invoice:
    invoice = Invoice.objects.create(
        organisation=organisation, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30)
    )
    InvoiceLine.objects.create(invoice=invoice, description="Plan", unit_net_cents=10_000)
    return invoice


@pytest.fixture
def alpha(db: None) -> Organisation:
    return customer("Alpha Studio")


@pytest.fixture
def owner_a(alpha: Organisation) -> User:
    return add_member(alpha, "alpha-owner")


@pytest.fixture
def owner_b(db: None) -> User:
    return add_member(customer("Bravo Creator"), "bravo-owner")


@pytest.fixture
def invoice_a(alpha: Organisation) -> Invoice:
    return issue(draft(alpha), on=date(2026, 9, 1))


def test_the_owner_lists_issued_documents_newest_first(
    owner_a: User, alpha: Organisation, invoice_a: Invoice
) -> None:
    paid = mark_paid(issue(draft(alpha), on=date(2026, 9, 2)), paid_on=date(2026, 9, 5))
    storno = cancel(invoice_a, on=date(2026, 9, 3))
    draft(alpha)  # never listed

    body = client_for(owner_a).get(f"{APP}/invoices").json()

    assert [row["number"] for row in body] == [storno.number, paid.number, invoice_a.number]
    by_number = {row["number"]: row for row in body}
    assert by_number[str(invoice_a.number)] | {"id": 0} == {
        "id": 0,
        "number": "2026-00001",
        "kind": "invoice",
        "status": "cancelled",
        "issued_on": "2026-09-01",
        "due_on": "2026-09-15",
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
        "currency": "EUR",
        "net_cents": 10_000,
        "vat_cents": 1_900,
        "gross_cents": 11_900,
        "vat_treatment": "domestic",
        "paid_on": None,
        "cancels_number": None,
        "cancelled_by_number": "2026-00003",
    }
    assert by_number[str(storno.number)]["status"] == "issued"
    assert by_number[str(storno.number)]["cancels_number"] == "2026-00001"
    assert by_number[str(storno.number)]["gross_cents"] == -11_900
    assert by_number[str(storno.number)]["due_on"] is None
    assert by_number[str(paid.number)]["status"] == "paid"
    assert by_number[str(paid.number)]["paid_on"] == "2026-09-05"


def test_an_unpaid_invoice_past_its_due_date_is_overdue(owner_a: User, invoice_a: Invoice) -> None:
    # Issued 2026-09-01 with 14 days to pay; the test runs long after that.
    [row] = client_for(owner_a).get(f"{APP}/invoices").json()
    assert row["status"] == "overdue"


def test_the_owner_downloads_the_stored_pdf(owner_a: User, invoice_a: Invoice) -> None:
    response = client_for(owner_a).get(f"{APP}/invoices/{invoice_a.public_id}/pdf")

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == 'attachment; filename="invoice-2026-00001.pdf"'
    assert response["Cache-Control"] == "private, no-store"
    assert "Access-Control-Allow-Origin" not in response
    assert response.content == bytes(Invoice.objects.get(pk=invoice_a.pk).pdf or b"")


def test_another_organisations_invoice_is_not_listed(owner_b: User, invoice_a: Invoice) -> None:
    response = client_for(owner_b).get(f"{APP}/invoices")
    assert (response.status_code, response.json()) == (200, [])


def test_a_foreign_pdf_a_draft_and_a_missing_id_are_the_same_404(
    owner_a: User, owner_b: User, alpha: Organisation, invoice_a: Invoice
) -> None:
    foreign = client_for(owner_b).get(f"{APP}/invoices/{invoice_a.public_id}/pdf")
    missing = client_for(owner_b).get(f"{APP}/invoices/{UNKNOWN_INVOICE}/pdf")
    unissued = client_for(owner_a).get(f"{APP}/invoices/{draft(alpha).public_id}/pdf")

    assert foreign.status_code == missing.status_code == unissued.status_code == 404
    assert foreign.content == missing.content == unissued.content
    assert b"2026-00001" not in foreign.content


def test_an_invoice_is_named_by_its_public_id_only(
    owner_a: User, alpha: Organisation, invoice_a: Invoice
) -> None:
    """ADR 0033: no primary key in the payload, and every other spelling is the one 404."""
    client = client_for(owner_a)
    [row] = client.get(f"{APP}/invoices").json()
    assert row["id"] == invoice_a.public_id
    assert public_ids.is_valid("inv", row["id"])

    missing = client.get(f"{APP}/invoices/{UNKNOWN_INVOICE}/pdf")
    other = issue(draft(customer("Charlie Studio")), on=date(2026, 9, 1))
    bad = (
        str(invoice_a.pk),  # the old integer URL of the caller's own invoice
        "0",
        "-1",
        "abc",
        str(invoice_a.number),
        invoice_a.public_id.upper(),
        invoice_a.public_id[:-1],
        invoice_a.public_id.replace("inv_", "orig_"),
        public_ids.missing("inv"),
        other.public_id,
    )
    assert missing.status_code == 404
    for value in bad:
        response = client.get(f"{APP}/invoices/{value}/pdf")
        assert response.status_code == 404, value
        assert response.content == missing.content, value
        assert response["Content-Type"] == missing["Content-Type"], value
    assert client.get(f"{APP}/invoices/{invoice_a.public_id}/pdf").status_code == 200


def test_staff_and_accounts_without_organisation_see_no_invoices(
    staff_client: Client, user_client: Client, invoice_a: Invoice
) -> None:
    for client in (staff_client, user_client):
        assert client.get(f"{APP}/invoices").json() == []
        assert client.get(f"{APP}/invoices/{invoice_a.public_id}/pdf").status_code == 404


def test_anonymous_requests_are_refused(client: Client, invoice_a: Invoice) -> None:
    assert client.get(f"{APP}/invoices").status_code == 401
    assert client.get(f"{APP}/invoices/{invoice_a.public_id}/pdf").status_code == 401


def test_a_colleague_sees_the_organisations_invoices(
    alpha: Organisation, invoice_a: Invoice
) -> None:
    from accounts.models import Membership

    colleague = add_member(alpha, "alpha-member", role=Membership.Role.MEMBER)
    assert [row["id"] for row in client_for(colleague).get(f"{APP}/invoices").json()] == [
        invoice_a.public_id
    ]
    assert client_for(colleague).get(f"{APP}/invoices/{invoice_a.public_id}/pdf").status_code == 200


def test_production_does_not_answer_invoices_yet(
    settings: Settings, owner_a: User, invoice_a: Invoice
) -> None:
    """Production holds no invoice data (ADR 0032): a bare 404, never an empty list."""
    settings.MARCELINE_ROLE = "prod"
    client = client_for(owner_a)
    for path in (f"{APP}/invoices", f"{APP}/invoices/{invoice_a.public_id}/pdf"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert b"2026-00001" not in response.content
