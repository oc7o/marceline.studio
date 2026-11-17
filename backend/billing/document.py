"""The invoice as a printed document: German with English subtitles. Pure: no Django, no database.

`render(document)` lays out one A4 page with what § 14 (4) UStG asks for — seller and buyer name
and address, the seller's tax number or VAT ID, issue date, invoice number, service description
and period, net amount, VAT rate and amount (or the reason there is none), gross amount — plus the
bank details and an EPC QR code for the transfer. A cancellation document names the invoice it
cancels and carries no payment request.

Wording that has legal weight (the § 19 and reverse-charge notes) lives in `NOTES`, in one place.
Changing it changes documents issued from then on only: issued PDFs are stored, not re-rendered.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from billing.epc import epc_payload, qr_matrix
from billing.pdf import A4_HEIGHT, A4_WIDTH, Page, build_pdf, text_width, wrap

NOTES = {
    "small_business": (
        "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.",
        "No VAT is charged under the German small-business scheme (§ 19 UStG).",
    ),
    "reverse_charge": (
        "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge).",
        "Reverse charge: VAT is payable by the recipient of the service.",
    ),
}

LEFT = 56.0
RIGHT = A4_WIDTH - 56.0
QR_MODULE = 1.9


@dataclass(frozen=True)
class Line:
    position: int
    description: str
    quantity: int
    unit_net_cents: int
    net_cents: int


@dataclass(frozen=True)
class Document:
    kind: str  # "invoice" | "cancellation"
    number: str
    issued_on: date
    due_on: date | None
    period_start: date | None
    period_end: date | None
    seller: dict[str, Any]
    buyer: dict[str, Any]
    lines: Sequence[Line]
    net_cents: int
    vat_cents: int
    gross_cents: int
    vat_treatment: str
    vat_rate_bp: int
    cancels_number: str | None = None
    cancels_issued_on: date | None = None


def money(cents: int) -> str:
    """German notation: 1.234,56 EUR."""
    sign = "-" if cents < 0 else ""
    euros, rest = divmod(abs(cents), 100)
    return f"{sign}{euros:,}".replace(",", ".") + f",{rest:02d} EUR"


def day(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def rate(rate_bp: int) -> str:
    whole, rest = divmod(rate_bp, 100)
    return f"{whole} %" if rest == 0 else f"{whole},{rest:02d}".rstrip("0") + " %"


def _block(page: Page, x: float, y: float, lines: Sequence[str], size: float = 9) -> float:
    for text in lines:
        page.text(x, y, text, size)
        y -= size + 3
    return y


def render(document: Document) -> bytes:
    page = Page()
    seller = document.seller
    buyer = document.buyer
    cancellation = document.kind == "cancellation"

    # Header: seller left, title right.
    y = A4_HEIGHT - 60
    page.text(LEFT, y, str(seller["name"]), 12, bold=True)
    _block(page, LEFT, y - 15, [*seller["address"]], 9)
    title = "Stornorechnung" if cancellation else "Rechnung"
    subtitle = "Cancellation invoice" if cancellation else "Invoice"
    page.text_right(RIGHT, y, title, 18, bold=True)
    page.text_right(RIGHT, y - 16, subtitle, 10)

    # Buyer.
    y = A4_HEIGHT - 170
    page.text(LEFT, y, "Rechnungsempfänger / Bill to", 8)
    buyer_lines = [str(buyer["name"]), *buyer["address"], str(buyer["country"])]
    if buyer.get("vat_id"):
        buyer_lines.append(f"USt-IdNr. / VAT ID: {buyer['vat_id']}")
    page.text(LEFT, y - 13, buyer_lines[0], 10, bold=True)
    _block(page, LEFT, y - 26, buyer_lines[1:], 9)

    # Facts, right column.
    facts = [
        ("Rechnungsnummer / Invoice no.", document.number),
        ("Rechnungsdatum / Invoice date", day(document.issued_on)),
    ]
    if document.period_start and document.period_end:
        facts.append(
            (
                "Leistungszeitraum / Service period",
                f"{day(document.period_start)} - {day(document.period_end)}",
            )
        )
    if document.due_on and not cancellation:
        facts.append(("Fällig am / Due date", day(document.due_on)))
    if cancellation and document.cancels_number:
        cancels = document.cancels_number
        if document.cancels_issued_on:
            cancels += f" ({day(document.cancels_issued_on)})"
        facts.append(("Storno zu / Cancels invoice", cancels))
    fy = y
    for label, value in facts:
        page.text(330, fy, label, 8)
        page.text_right(RIGHT, fy - 11, value, 9, bold=True)
        fy -= 26

    # Positions.
    y = min(y - 26 - 12 * len(buyer_lines), fy) - 20
    # Right edges of the quantity, unit price and amount columns; the description wraps before them.
    description_x, description_width = LEFT + 28, 200.0
    quantity_right, unit_right = 350.0, 460.0
    page.text(LEFT, y, "Pos.", 8, bold=True)
    page.text(description_x, y, "Beschreibung / Description", 8, bold=True)
    page.text_right(quantity_right, y, "Menge / Qty", 8, bold=True)
    page.text_right(unit_right, y, "Einzelpreis / Unit", 8, bold=True)
    page.text_right(RIGHT, y, "Betrag / Amount", 8, bold=True)
    y -= 6
    page.line(LEFT, y, RIGHT, y)
    y -= 13
    for line in document.lines:
        page.text(LEFT, y, str(line.position), 9)
        page.text_right(quantity_right, y, str(line.quantity), 9)
        page.text_right(unit_right, y, money(line.unit_net_cents), 9)
        page.text_right(RIGHT, y, money(line.net_cents), 9)
        for text in wrap(line.description, description_width, 9):
            page.text(description_x, y, text, 9)
            y -= 12
        y -= 3
    page.line(LEFT, y + 6, RIGHT, y + 6)

    # Totals.
    y -= 10
    totals = [("Nettobetrag / Net amount", money(document.net_cents), False)]
    if document.vat_treatment == "domestic":
        totals.append(
            (f"Umsatzsteuer / VAT {rate(document.vat_rate_bp)}", money(document.vat_cents), False)
        )
    else:
        totals.append(("Umsatzsteuer / VAT", money(0), False))
    totals.append(("Gesamtbetrag / Total", money(document.gross_cents), True))
    for label, value, bold in totals:
        page.text(330, y, label, 9, bold=bold)
        page.text_right(RIGHT, y, value, 9, bold=bold)
        y -= 14

    # Notes.
    y -= 8
    notes = list(NOTES.get(document.vat_treatment, ()))
    if document.vat_treatment == "reverse_charge" and seller.get("vat_id"):
        notes.append(f"USt-IdNr. Leistender / Supplier VAT ID: {seller['vat_id']}")
    if cancellation and document.cancels_number:
        notes.append(
            f"Diese Stornorechnung hebt die Rechnung {document.cancels_number} vollständig auf."
        )
        notes.append(f"This document cancels invoice {document.cancels_number} in full.")
    for note in notes:
        for text in wrap(note, RIGHT - LEFT, 9):
            page.text(LEFT, y, text, 9)
            y -= 12

    if not cancellation and document.gross_cents > 0:
        _payment(page, document, y - 14)

    # Footer.
    footer = [str(seller["name"])]
    if seller.get("tax_number"):
        footer.append(f"Steuernummer: {seller['tax_number']}")
    if seller.get("vat_id"):
        footer.append(f"USt-IdNr.: {seller['vat_id']}")
    if seller.get("email"):
        footer.append(str(seller["email"]))
    page.line(LEFT, 58, RIGHT, 58)
    footer_text = " · ".join(footer)
    for index, text in enumerate(wrap(footer_text, RIGHT - LEFT, 7.5)):
        page.text(LEFT, 46 - index * 10, text, 7.5)

    return build_pdf(page, title=f"{title} {document.number}")


def _payment(page: Page, document: Document, y: float) -> None:
    seller = document.seller
    lead = (
        f"Bitte überweisen Sie {money(document.gross_cents)} bis zum {day(document.due_on)} "
        f"unter Angabe der Rechnungsnummer {document.number}."
        if document.due_on
        else f"Bitte überweisen Sie {money(document.gross_cents)} unter Angabe der Rechnungsnummer."
    )
    english = (
        f"Please transfer {money(document.gross_cents)} by {day(document.due_on)}, quoting "
        f"invoice number {document.number}."
        if document.due_on
        else f"Please transfer {money(document.gross_cents)}, quoting the invoice number."
    )
    text_right_edge = RIGHT - 120
    for paragraph in (lead, english):
        for text in wrap(paragraph, text_right_edge - LEFT, 9):
            page.text(LEFT, y, text, 9)
            y -= 12
    y -= 4
    rows = [
        ("Kontoinhaber / Account holder", str(seller["name"])),
        ("IBAN", _group(str(seller["iban"]))),
    ]
    if seller.get("bic"):
        rows.append(("BIC", str(seller["bic"])))
    if seller.get("bank"):
        rows.append(("Bank", str(seller["bank"])))
    top = y + 12
    for label, value in rows:
        page.text(LEFT, y, label, 8)
        page.text(LEFT + 140, y, value, 9, bold=True)
        y -= 13

    payload = epc_payload(
        name=str(seller["name"]),
        iban=str(seller["iban"]),
        bic=str(seller.get("bic") or ""),
        amount_cents=document.gross_cents,
        remittance=document.number,
    )
    matrix = qr_matrix(payload)
    size = len(matrix) * QR_MODULE
    x0 = RIGHT - size
    y0 = top - size + 20
    for row_index, row in enumerate(matrix):
        # Merge horizontal runs of dark modules into one rectangle each.
        start: int | None = None
        for column, dark in enumerate([*row, False]):
            if dark and start is None:
                start = column
            elif not dark and start is not None:
                page.rect(
                    x0 + start * QR_MODULE,
                    y0 + size - (row_index + 1) * QR_MODULE,
                    (column - start) * QR_MODULE,
                    QR_MODULE,
                )
                start = None
    caption = "GiroCode"
    page.text(x0 + (size - text_width(caption, 7)) / 2, y0 - 10, caption, 7)


def _group(iban: str) -> str:
    return " ".join(iban[index : index + 4] for index in range(0, len(iban), 4))
