"""The printed invoice: a well-formed PDF with the mandatory facts and a scannable transfer code."""

import re
import zlib
from datetime import date

import pytest

from billing.document import Document, Line, money, rate, render
from billing.epc import epc_payload, qr_matrix
from billing.pdf import Page, build_pdf, text_width, wrap
from billing.seller import iban_is_valid

SELLER = {
    "name": "Example Seller",
    "address": ["Example Street 1", "12345 Example City"],
    "tax_number": "00/000/00000",
    "vat_id": "DE000000000",
    "email": "billing@example.test",
    "iban": "DE89370400440532013000",
    "bic": "COBADEFFXXX",
    "bank": "Example Bank",
    "small_business": False,
}
BUYER = {
    "name": "Studio GmbH",
    "address": ["Example Road 2", "54321 Example Town"],
    "country": "DE",
    "is_business": True,
    "vat_id": "",
}


def _document(**overrides: object) -> Document:
    values: dict[str, object] = {
        "kind": "invoice",
        "number": "2026-00001",
        "issued_on": date(2026, 9, 17),
        "due_on": date(2026, 10, 1),
        "period_start": date(2026, 9, 17),
        "period_end": date(2026, 10, 16),
        "seller": SELLER,
        "buyer": BUYER,
        "lines": [Line(1, "Marceline Professional (monthly)", 1, 10_000, 10_000)],
        "net_cents": 10_000,
        "vat_cents": 1_900,
        "gross_cents": 11_900,
        "vat_treatment": "domestic",
        "vat_rate_bp": 1900,
    }
    values.update(overrides)
    return Document(**values)  # type: ignore[arg-type]


def page_text(pdf: bytes) -> str:
    """Every string drawn on the page, decoded from the compressed content stream."""
    stream = re.search(rb"stream\n(.*)\nendstream", pdf, re.S)
    assert stream is not None
    content = zlib.decompress(stream.group(1))
    strings = re.findall(rb"\(((?:[^()\\]|\\.)*)\) Tj", content)
    return "\n".join(re.sub(rb"\\(.)", rb"\1", raw).decode("cp1252") for raw in strings)


def test_the_pdf_is_well_formed() -> None:
    pdf = render(_document())

    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")
    startxref = int(re.findall(rb"startxref\n(\d+)", pdf)[-1])
    assert pdf[startxref:].startswith(b"xref")
    offsets = [int(value) for value in re.findall(rb"(\d{10}) 00000 n", pdf)]
    assert len(offsets) == 7
    for number, offset in enumerate(offsets, start=1):
        assert pdf[offset:].startswith(f"{number} 0 obj".encode())


def test_rendering_is_deterministic() -> None:
    assert render(_document()) == render(_document())


def test_the_invoice_carries_the_mandatory_facts() -> None:
    text = page_text(render(_document()))

    for fact in (
        "Example Seller",
        "Example Street 1",
        "Studio GmbH",
        "54321 Example Town",
        "2026-00001",
        "17.09.2026",
        "17.09.2026 - 16.10.2026",
        "01.10.2026",
        "Marceline Professional (monthly)",
        "100,00 EUR",
        "Umsatzsteuer / VAT 19 %",
        "19,00 EUR",
        "119,00 EUR",
        "Steuernummer: 00/000/00000",
        "DE89 3704 0044 0532 0130 00",
        "COBADEFFXXX",
    ):
        assert fact in text, fact


def test_reverse_charge_names_both_vat_ids_and_the_note() -> None:
    buyer = {**BUYER, "country": "AT", "vat_id": "ATU12345678"}
    text = page_text(
        render(
            _document(
                buyer=buyer,
                vat_treatment="reverse_charge",
                vat_rate_bp=0,
                vat_cents=0,
                gross_cents=10_000,
            )
        )
    )

    assert "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge)." in text
    assert "ATU12345678" in text
    assert "DE000000000" in text
    assert "VAT 19 %" not in text


def test_small_business_invoice_says_why_there_is_no_vat() -> None:
    seller = {**SELLER, "vat_id": "", "small_business": True}
    text = page_text(
        render(
            _document(
                seller=seller,
                vat_treatment="small_business",
                vat_rate_bp=0,
                vat_cents=0,
                gross_cents=10_000,
            )
        )
    )

    assert "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet." in text
    assert "USt-IdNr." not in text


def test_a_cancellation_names_its_invoice_and_asks_for_no_payment() -> None:
    text = page_text(
        render(
            _document(
                kind="cancellation",
                number="2026-00002",
                due_on=None,
                lines=[Line(1, "Marceline Professional (monthly)", 1, -10_000, -10_000)],
                net_cents=-10_000,
                vat_cents=-1_900,
                gross_cents=-11_900,
                cancels_number="2026-00001",
                cancels_issued_on=date(2026, 9, 17),
            )
        )
    )

    assert "Stornorechnung" in text
    assert "2026-00001 (17.09.2026)" in text
    assert "-119,00 EUR" in text
    assert "Bitte überweisen" not in text
    assert "IBAN" not in text


def test_characters_outside_windows_1252_do_not_break_the_pdf() -> None:
    buyer = {**BUYER, "name": "Studio Łódź (Ω)"}
    text = page_text(render(_document(buyer=buyer)))
    assert "Studio ?ód? (?)" in text


def test_money_rate_and_widths() -> None:
    assert money(123_456_78) == "123.456,78 EUR"
    assert money(-5) == "-0,05 EUR"
    assert rate(1900) == "19 %"
    assert rate(750) == "7,5 %"
    assert text_width("0", 10) == pytest.approx(5.56)
    assert wrap("aaa bbb ccc", text_width("aaa bbb", 9), 9) == ["aaa bbb", "ccc"]


def test_build_pdf_escapes_parentheses() -> None:
    page = Page()
    page.text(10, 10, "a (b) \\ c")
    assert page_text(build_pdf(page, title="t")) == "a (b) \\ c"


def test_epc_payload_follows_version_002() -> None:
    payload = epc_payload(
        name="Example Seller",
        iban="DE89370400440532013000",
        bic="COBADEFFXXX",
        amount_cents=11_900,
        remittance="2026-00001",
    )
    assert payload.split("\n") == [
        "BCD",
        "002",
        "1",
        "SCT",
        "COBADEFFXXX",
        "Example Seller",
        "DE89370400440532013000",
        "EUR119.00",
        "",
        "",
        "2026-00001",
    ]
    matrix = qr_matrix(payload)
    assert len(matrix) == len(matrix[0]) and len(matrix) <= 69  # version 13 at most


@pytest.mark.parametrize("cents", [0, -1, 100_000_000_000])
def test_epc_refuses_impossible_amounts(cents: int) -> None:
    with pytest.raises(ValueError):
        epc_payload(
            name="x", iban="DE89370400440532013000", bic="", amount_cents=cents, remittance=""
        )


def test_iban_check_digits() -> None:
    assert iban_is_valid("DE89370400440532013000")
    assert not iban_is_valid("DE88370400440532013000")
    assert not iban_is_valid("not an iban")
