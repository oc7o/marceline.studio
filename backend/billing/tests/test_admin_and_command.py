from collections.abc import Callable
from datetime import date
from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse

from accounts.models import Organisation
from billing.models import Invoice, InvoiceLine, Plan, Subscription
from billing.services import issue

pytestmark = pytest.mark.usefixtures("seller")


@pytest.fixture
def admin_client_(db: None) -> Client:
    client = Client()
    client.force_login(User.objects.create_superuser("operator", password="secret-pass-123"))
    return client


def _draft(organisation: Organisation) -> Invoice:
    invoice = Invoice.objects.create(organisation=organisation)
    InvoiceLine.objects.create(invoice=invoice, description="Service", unit_net_cents=10_000)
    return invoice


def test_command_is_idempotent_and_dry_run_first(
    make_customer: Callable[..., Organisation], monthly_plan: Plan
) -> None:
    Subscription.objects.create(
        organisation=make_customer(), plan=monthly_plan, starts_on=date(2026, 9, 1)
    )
    out = StringIO()

    call_command("issue_invoices", "--dry-run", "--date", "2026-09-17", stdout=out)
    assert "would issue 1 invoice(s)" in out.getvalue()
    assert not Invoice.objects.exists()

    call_command("issue_invoices", "--date", "2026-09-17", stdout=out)
    call_command("issue_invoices", "--date", "2026-09-17", stdout=out)
    assert Invoice.objects.count() == 1
    assert "issued: 2026-00001" in out.getvalue()
    assert "issued 0 invoice(s)" in out.getvalue()


def test_command_fails_loudly_on_unbillable_subscriptions(
    make_customer: Callable[..., Organisation], monthly_plan: Plan
) -> None:
    Subscription.objects.create(
        organisation=make_customer(country="CH"), plan=monthly_plan, starts_on=date(2026, 9, 1)
    )
    with pytest.raises(CommandError, match="could not be invoiced"):
        call_command("issue_invoices", "--date", "2026-09-17", stdout=StringIO(), stderr=StringIO())


def test_admin_issue_mark_paid_and_download(
    admin_client_: Client, make_customer: Callable[..., Organisation]
) -> None:
    invoice = _draft(make_customer())
    changelist = reverse("admin:billing_invoice_changelist")

    admin_client_.post(
        changelist, {"action": "issue_drafts", "_selected_action": [invoice.pk]}, follow=True
    )
    invoice.refresh_from_db()
    assert invoice.number is not None and invoice.number.endswith("-00001")

    admin_client_.post(
        changelist, {"action": "mark_paid_today", "_selected_action": [invoice.pk]}, follow=True
    )
    invoice.refresh_from_db()
    assert invoice.paid_on is not None

    response = admin_client_.get(reverse("admin:billing_invoice_pdf", args=(invoice.pk,)))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")

    page = admin_client_.get(reverse("admin:billing_invoice_change", args=(invoice.pk,)))
    assert page.status_code == 200
    assert b'name="paid_on"' in page.content
    assert b'name="organisation"' not in page.content


def test_admin_cannot_delete_an_issued_invoice(
    admin_client_: Client, make_customer: Callable[..., Organisation]
) -> None:
    invoice = issue(_draft(make_customer()), on=date(2026, 9, 17))
    response = admin_client_.post(
        reverse("admin:billing_invoice_delete", args=(invoice.pk,)), {"post": "yes"}
    )
    assert response.status_code == 403
    assert Invoice.objects.filter(pk=invoice.pk).exists()


def test_admin_cancel_action_issues_a_storno(
    admin_client_: Client, make_customer: Callable[..., Organisation]
) -> None:
    invoice = issue(_draft(make_customer()), on=date(2026, 9, 17))
    admin_client_.post(
        reverse("admin:billing_invoice_changelist"),
        {"action": "cancel_invoices", "_selected_action": [invoice.pk]},
        follow=True,
    )
    assert Invoice.objects.filter(kind="cancellation", cancels=invoice).count() == 1
