"""Billing workflows: issue, cancel, record payment, bill subscriptions. The only invoice writer.

Every refusal is a `BillingError` with a readable reason for the admin and the command line. The
database triggers (migration 0002) refuse the same illegal writes again, for any other path.
"""

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from accounts.models import Organisation
from billing import document, numbering
from billing.models import BillingProfile, Invoice, InvoiceLine, Subscription
from billing.periods import Period, due_periods
from billing.seller import Seller, seller_from_settings, seller_problems
from billing.vat import VatNotSupported, decide, normalise_vat_id, vat_cents

logger = logging.getLogger("marceline.billing")

# Invoice dates are German calendar days, whatever the server's time zone.
BILLING_TIME_ZONE = ZoneInfo("Europe/Berlin")

InvoiceStatus = Literal["draft", "open", "overdue", "paid", "cancelled", "issued"]


class BillingError(Exception):
    """A billing action was refused. The message says why."""


def today() -> date:
    return timezone.now().astimezone(BILLING_TIME_ZONE).date()


def status_of(invoice: Invoice, on: date) -> InvoiceStatus:
    """Derived, never stored: an issued row may not change, but its standing does.

    `issued` is the standing of a cancellation document, which asks for no payment of its own.
    A cancelled invoice is `cancelled` even if it had been paid (refunds are handled by hand).
    """
    if invoice.number is None:
        return "draft"
    if invoice.kind == Invoice.Kind.CANCELLATION:
        return "issued"
    if hasattr(invoice, "cancellation"):
        return "cancelled"
    if invoice.paid_on is not None:
        return "paid"
    if invoice.due_on is not None and invoice.due_on < on:
        return "overdue"
    return "open"


def issued_invoices(organisation: Organisation | None) -> QuerySet[Invoice]:
    """The issued documents of one organisation, newest first. Drafts are never shown to anyone
    outside the admin. `None` (no organisation) has none."""
    if organisation is None:
        return Invoice.objects.none()
    return Invoice.objects.filter(organisation=organisation, number__isnull=False).order_by(
        "-issued_on", "-pk"
    )


def _seller() -> Seller:
    seller = seller_from_settings()
    problems = seller_problems(seller)
    if problems:
        raise BillingError("Seller settings incomplete: " + "; ".join(problems))
    return seller


def _buyer(organisation: Organisation) -> dict[str, Any]:
    profile = BillingProfile.objects.filter(organisation=organisation).first()
    if profile is None:
        raise BillingError(f"Organisation {organisation.pk} has no billing profile")
    return {
        "name": profile.legal_name,
        "address": [line.strip() for line in profile.address.splitlines() if line.strip()],
        "country": profile.country,
        "is_business": profile.is_business,
        "vat_id": normalise_vat_id(profile.vat_id),
    }


def _document(invoice: Invoice, lines: Iterable[InvoiceLine]) -> document.Document:
    assert invoice.number is not None and invoice.issued_on is not None
    assert invoice.net_cents is not None and invoice.vat_cents is not None
    assert invoice.gross_cents is not None and invoice.vat_rate_bp is not None
    cancels = invoice.cancels
    return document.Document(
        kind=invoice.kind,
        number=invoice.number,
        issued_on=invoice.issued_on,
        due_on=invoice.due_on,
        period_start=invoice.period_start,
        period_end=invoice.period_end,
        seller=dict(invoice.seller or {}),
        buyer=dict(invoice.buyer or {}),
        lines=[
            document.Line(
                position=line.position,
                description=line.description,
                quantity=line.quantity,
                unit_net_cents=line.unit_net_cents,
                net_cents=line.net_cents,
            )
            for line in lines
        ],
        net_cents=invoice.net_cents,
        vat_cents=invoice.vat_cents,
        gross_cents=invoice.gross_cents,
        vat_treatment=invoice.vat_treatment,
        vat_rate_bp=invoice.vat_rate_bp,
        cancels_number=cancels.number if cancels else None,
        cancels_issued_on=cancels.issued_on if cancels else None,
    )


def _finish(invoice: Invoice, lines: list[InvoiceLine], seller: Seller, on: date) -> Invoice:
    """Number, render and store the issue-time columns. Inside the issuing transaction."""
    invoice.number = numbering.next_number(on.year)
    invoice.issued_on = on
    invoice.seller = seller.snapshot()
    invoice.pdf = document.render(_document(invoice, lines))
    invoice.pdf_sha256 = hashlib.sha256(invoice.pdf).hexdigest()
    invoice.save()
    logger.info("billing: issued %s %s", invoice.kind, invoice.number)
    return invoice


def issue(invoice: Invoice, *, on: date | None = None) -> Invoice:
    """Turn a draft invoice into an issued, numbered, immutable one."""
    on = on or today()
    seller = _seller()
    with transaction.atomic():
        locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if locked.number is not None:
            raise BillingError(f"Invoice {locked.number} is already issued")
        if locked.kind != Invoice.Kind.INVOICE:
            raise BillingError("Cancellations are issued with cancel(), not issue()")
        lines = list(locked.lines.all())
        if not lines:
            raise BillingError("An invoice needs at least one line")
        buyer = _buyer(locked.organisation)
        assert seller.small_business is not None  # seller_problems guarantees it
        try:
            decision = decide(
                seller_small_business=seller.small_business,
                seller_vat_id=seller.vat_id,
                buyer_country=buyer["country"],
                buyer_is_business=buyer["is_business"],
                buyer_vat_id=buyer["vat_id"],
            )
        except VatNotSupported as refusal:
            raise BillingError(f"VAT case not supported: {refusal.code}") from refusal
        net = sum(line.net_cents for line in lines)
        if net < 0:
            raise BillingError("An invoice total cannot be negative; issue a cancellation instead")
        locked.buyer = buyer
        locked.vat_treatment = decision.treatment
        locked.vat_rate_bp = decision.rate_bp
        locked.net_cents = net
        locked.vat_cents = vat_cents(net, decision.rate_bp)
        locked.gross_cents = net + locked.vat_cents
        locked.due_on = on + timedelta(days=int(settings.BILLING_PAYMENT_TERM_DAYS))
        return _finish(locked, lines, seller, on)


def cancel(invoice: Invoice, *, on: date | None = None) -> Invoice:
    """Issue the cancellation document (Storno) of an issued invoice. The invoice stays as it was.

    The cancellation mirrors the original exactly — same buyer, lines, VAT treatment and rate,
    negated amounts — so it cancels what was invoiced even if settings changed since.
    """
    on = on or today()
    seller = _seller()
    with transaction.atomic():
        original = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if original.number is None:
            raise BillingError("A draft is deleted, not cancelled")
        if original.kind != Invoice.Kind.INVOICE:
            raise BillingError("A cancellation cannot be cancelled")
        if Invoice.objects.filter(cancels=original).exists():
            raise BillingError(f"Invoice {original.number} is already cancelled")
        assert original.net_cents is not None and original.vat_cents is not None
        assert original.gross_cents is not None
        storno = Invoice.objects.create(
            organisation=original.organisation,
            kind=Invoice.Kind.CANCELLATION,
            cancels=original,
            period_start=original.period_start,
            period_end=original.period_end,
        )
        lines = [
            InvoiceLine.objects.create(
                invoice=storno,
                position=line.position,
                description=line.description,
                quantity=line.quantity,
                unit_net_cents=-line.unit_net_cents,
            )
            for line in original.lines.all()
        ]
        storno.buyer = original.buyer
        storno.vat_treatment = original.vat_treatment
        storno.vat_rate_bp = original.vat_rate_bp
        storno.net_cents = -original.net_cents
        storno.vat_cents = -original.vat_cents
        storno.gross_cents = -original.gross_cents
        try:
            return _finish(storno, lines, seller, on)
        except IntegrityError as error:  # pragma: no cover - the check above ran under a lock
            raise BillingError(f"Invoice {original.number} is already cancelled") from error


def mark_paid(invoice: Invoice, *, paid_on: date, note: str = "") -> Invoice:
    """Record that the transfer arrived (bank reconciliation by hand)."""
    with transaction.atomic():
        locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if locked.number is None or locked.kind != Invoice.Kind.INVOICE:
            raise BillingError("Only an issued invoice can be marked paid")
        locked.paid_on = paid_on
        locked.payment_note = note[:200]
        locked.save(update_fields=["paid_on", "payment_note"])
        return locked


def mark_unpaid(invoice: Invoice) -> Invoice:
    """Undo a payment recorded by mistake."""
    with transaction.atomic():
        locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
        locked.paid_on = None
        locked.payment_note = ""
        locked.save(update_fields=["paid_on", "payment_note"])
        return locked


def draft_for_period(subscription: Subscription, period: Period) -> Invoice:
    plan = subscription.plan
    invoice = Invoice.objects.create(
        organisation=subscription.organisation,
        subscription=subscription,
        period_start=period.start,
        period_end=period.end,
    )
    InvoiceLine.objects.create(
        invoice=invoice,
        position=1,
        description=plan.name,
        quantity=1,
        unit_net_cents=plan.price_net_cents,
    )
    return invoice


@dataclass
class IssueRun:
    issued: list[str] = field(default_factory=list)
    due: list[tuple[int, Period]] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)


def issue_due_invoices(*, on: date | None = None, dry_run: bool = False) -> IssueRun:
    """Issue one invoice per started, not yet invoiced period of every subscription. Idempotent.

    A period already invoiced — cancelled or not — is never invoiced again: a correction after a
    cancellation is a manual invoice. One transaction per invoice; a subscription that cannot be
    invoiced (no billing profile, an unsupported VAT case) is reported and the others go on.
    """
    on = on or today()
    run = IssueRun()
    if not dry_run:
        _seller()  # refuse the whole run up front rather than once per subscription
    subscriptions = Subscription.objects.select_related("plan", "organisation").filter(
        starts_on__lte=on
    )
    for subscription in subscriptions.order_by("pk"):
        invoiced = set(
            Invoice.objects.filter(
                subscription=subscription, kind=Invoice.Kind.INVOICE
            ).values_list("period_start", flat=True)
        )
        periods = due_periods(
            subscription.starts_on,
            subscription.plan.interval,  # type: ignore[arg-type]
            today=on,
            ends_on=subscription.ends_on,
        )
        for period in periods:
            if period.start in invoiced:
                continue
            run.due.append((subscription.pk, period))
            if dry_run:
                continue
            try:
                with transaction.atomic():
                    Subscription.objects.select_for_update().get(pk=subscription.pk)
                    if Invoice.objects.filter(
                        subscription=subscription,
                        kind=Invoice.Kind.INVOICE,
                        period_start=period.start,
                    ).exists():
                        continue  # a concurrent run got there first
                    issued = issue(draft_for_period(subscription, period), on=on)
                    run.issued.append(str(issued.number))
            except BillingError as error:
                run.failed.append((subscription.pk, str(error)))
                break  # later periods of this subscription would fail the same way
    return run
