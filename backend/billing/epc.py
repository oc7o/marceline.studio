"""The EPC QR code ("GiroCode") on an invoice: a SEPA credit transfer a banking app can scan.

Payload per EPC069-12 version 002, UTF-8, error correction level M, at most QR version 13 and
331 bytes. The BIC is optional inside the EEA in version 002 and is included when configured.
The remittance text is the invoice number, so a transfer can be matched to its invoice by hand.
"""

import segno

MAX_PAYLOAD_BYTES = 331
MAX_NAME = 70
MAX_REMITTANCE = 140


def epc_payload(*, name: str, iban: str, bic: str, amount_cents: int, remittance: str) -> str:
    if amount_cents < 1 or amount_cents > 99_999_999_999:
        raise ValueError("EPC amount must be between 0.01 and 999999999.99 EUR")
    lines = [
        "BCD",
        "002",
        "1",  # UTF-8
        "SCT",
        bic,
        name[:MAX_NAME],
        iban,
        f"EUR{amount_cents // 100}.{amount_cents % 100:02d}",
        "",  # purpose code
        "",  # structured creditor reference
        remittance[:MAX_REMITTANCE],
    ]
    payload = "\n".join(lines)
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("EPC payload longer than 331 bytes")
    return payload


def qr_matrix(payload: str) -> list[list[bool]]:
    """Dark modules of the QR symbol, row by row, without the quiet zone."""
    code = segno.make_qr(payload, error="m", boost_error=False, encoding="utf-8")
    if not isinstance(code.version, int) or code.version > 13:
        raise ValueError("EPC QR codes are limited to version 13")
    return [[bool(module) for module in row] for row in code.matrix]
