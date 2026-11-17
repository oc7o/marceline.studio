"""The organisation's invoices (ADR 0032): `GET /api/v1/app/invoices` and the PDF of one.

Read-only. Payment is by bank transfer against the invoice, so there is nothing to submit here.

The customer API's rules hold as everywhere in this package:
- **Scope through the organisation.** Both views start at `billing.services.issued_invoices`
  for the caller's organisation, and a draft is not an invoice yet — never listed, never served.
- **404, never 403.** Another organisation's invoice, a draft and an id that does not exist get
  the same status and byte-identical body — and so do a malformed id and an integer.
- **Public ids only** (ADR 0033). A document is named by `Invoice.public_id` (`inv_…`), never by
  its primary key. The legal `number` is sequential by law and printed on the PDF anyway; it is
  shown, but never used to address a document.
- **Codes, not words.** Status, kind and VAT treatment are codes; the UI owns the language. The
  PDF is the document itself, in German with English subtitles, exactly as issued.
"""

import re

from django.http import HttpRequest, HttpResponse
from ninja import Router, Status

from billing.models import Invoice
from billing.services import issued_invoices, status_of, today
from core import public_ids
from marceline.app.auth import customer_auth
from marceline.app.ownership import organisation_of
from marceline.app.schemas import AppErrorOut, CustomerInvoiceOut

router = Router(tags=["app"], auth=customer_auth)

# The same answer for "does not exist", "is a draft" and "belongs to someone else".
NOT_FOUND = "Invoice not found"
#: The kind of public id that names an invoice or cancellation document.
INVOICE_ID_PREFIX = "inv"

PDF_OPENAPI = {
    "responses": {
        "200": {
            "description": "The issued invoice as stored (application/pdf, attachment).",
            "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
        }
    }
}


def invoice_out(invoice: Invoice) -> CustomerInvoiceOut:
    assert invoice.number is not None and invoice.issued_on is not None
    assert invoice.net_cents is not None and invoice.vat_cents is not None
    assert invoice.gross_cents is not None
    cancellation = getattr(invoice, "cancellation", None)
    return CustomerInvoiceOut(
        id=invoice.public_id,
        number=invoice.number,
        kind=invoice.kind,  # type: ignore[arg-type]
        status=status_of(invoice, today()),  # type: ignore[arg-type]
        issued_on=invoice.issued_on,
        due_on=invoice.due_on if invoice.kind == Invoice.Kind.INVOICE else None,
        period_start=invoice.period_start,
        period_end=invoice.period_end,
        currency="EUR",
        net_cents=invoice.net_cents,
        vat_cents=invoice.vat_cents,
        gross_cents=invoice.gross_cents,
        vat_treatment=invoice.vat_treatment,  # type: ignore[arg-type]
        paid_on=invoice.paid_on,
        cancels_number=invoice.cancels.number if invoice.cancels else None,
        cancelled_by_number=cancellation.number if cancellation else None,
    )


@router.get("", response=list[CustomerInvoiceOut])
def invoices(request: HttpRequest) -> list[CustomerInvoiceOut]:
    """Every issued invoice and cancellation of this organisation, newest first."""
    rows = (
        issued_invoices(organisation_of(request.user))
        .select_related("cancels", "cancellation")
        .defer("pdf", "seller", "buyer", "cancels__pdf", "cancels__seller", "cancels__buyer")
        .defer("cancellation__pdf", "cancellation__seller", "cancellation__buyer")
    )
    return [invoice_out(row) for row in rows]


@router.get(
    "/{invoice_id}/pdf",
    response={404: AppErrorOut},
    openapi_extra=PDF_OPENAPI,
)
def invoice_pdf(request: HttpRequest, invoice_id: str) -> HttpResponse | Status[AppErrorOut]:
    """The stored PDF of one issued document, by its public id.

    404 unless this organisation's and issued — the same body for a malformed, integer, unknown,
    draft or foreign id.
    """
    if not public_ids.is_valid(INVOICE_ID_PREFIX, invoice_id):
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    invoice = (
        issued_invoices(organisation_of(request.user))
        .only("number", "pdf")
        .filter(public_id=invoice_id)
        .first()
    )
    if invoice is None or invoice.pdf is None or invoice.number is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", invoice.number)
    response = HttpResponse(bytes(invoice.pdf), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="invoice-{filename}.pdf"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
