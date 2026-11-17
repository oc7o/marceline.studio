"""A deliberately tiny PDF writer: one A4 page, Helvetica, text, lines and filled rectangles.

Invoices need nothing more, and this keeps a PDF library (and its font tooling) out of the
dependencies. The two standard Helvetica fonts every PDF reader ships are used with
WinAnsiEncoding, which covers German (ä ö ü ß), € and §; a character outside Windows-1252 is
printed as "?". The output is deterministic — no creation date, no random ids — so the same
document renders to the same bytes. Pure: no Django.

Coordinates are PDF points from the bottom-left corner (A4 = 595 x 842).
"""

import unicodedata
import zlib
from dataclasses import dataclass, field

A4_WIDTH = 595.0
A4_HEIGHT = 842.0

# Advance widths (1/1000 em) of the printable ASCII range 32..126, from Adobe's Helvetica AFMs.
_REGULAR = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 "
    "556 556 556 278 278 584 584 584 556 1015 667 667 722 722 667 611 778 722 278 500 667 556 833 "
    "722 778 667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 333 556 556 500 556 "
    "556 278 556 556 222 222 500 222 833 556 556 556 556 333 500 278 556 500 722 500 500 500 334 "
    "260 334 584"
)
_BOLD = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 "
    "556 556 556 333 333 584 584 584 611 975 722 722 722 722 667 611 778 722 278 556 722 611 833 "
    "722 778 667 778 722 667 611 722 667 944 667 667 611 333 278 333 584 556 333 556 611 556 611 "
    "556 333 611 611 278 278 556 278 889 611 611 611 611 389 556 333 611 556 778 556 556 500 389 "
    "280 389 584"
)
WIDTHS = {
    False: {chr(32 + index): int(width) for index, width in enumerate(_REGULAR.split())},
    True: {chr(32 + index): int(width) for index, width in enumerate(_BOLD.split())},
}
# Characters outside ASCII that invoices use; everything else falls back to its base letter.
_EXTRA = {"ß": 611, "€": 556, "§": 556, "\u2013": 556, "·": 278, "°": 400}


def char_width(char: str, bold: bool) -> int:
    table = WIDTHS[bold]
    if char in table:
        return table[char]
    if char in _EXTRA:
        return _EXTRA[char]
    base = unicodedata.normalize("NFD", char)[:1]
    return table.get(base, 556)


def text_width(text: str, size: float, *, bold: bool = False) -> float:
    return sum(char_width(char, bold) for char in text) * size / 1000


def wrap(text: str, width: float, size: float, *, bold: bool = False) -> list[str]:
    """Greedy word wrap to `width` points; a single over-long word is cut."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if text_width(candidate, size, bold=bold) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        while text_width(word, size, bold=bold) > width:
            cut = len(word)
            while cut > 1 and text_width(word[:cut], size, bold=bold) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        lines.append(current)
    return lines or [""]


def _encode(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _num(value: float) -> bytes:
    return f"{value:.2f}".rstrip("0").rstrip(".").encode() or b"0"


@dataclass
class Page:
    """Drawing operations for one page, in order."""

    ops: list[bytes] = field(default_factory=list)

    def text(self, x: float, y: float, text: str, size: float = 9, *, bold: bool = False) -> None:
        font = b"/F2" if bold else b"/F1"
        self.ops.append(
            b"BT " + font + b" " + _num(size) + b" Tf " + _num(x) + b" " + _num(y) + b" Td ("
            + _encode(text) + b") Tj ET"
        )  # fmt: skip

    def text_right(
        self, right: float, y: float, text: str, size: float = 9, *, bold: bool = False
    ) -> None:
        self.text(right - text_width(text, size, bold=bold), y, text, size, bold=bold)

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.5) -> None:
        self.ops.append(
            _num(width) + b" w " + _num(x1) + b" " + _num(y1) + b" m " + _num(x2) + b" "
            + _num(y2) + b" l S"
        )  # fmt: skip

    def rect(self, x: float, y: float, width: float, height: float) -> None:
        """A filled black rectangle."""
        self.ops.append(
            _num(x) + b" " + _num(y) + b" " + _num(width) + b" " + _num(height) + b" re f"
        )

    def content(self) -> bytes:
        return b"\n".join(self.ops)


def _pdf_string(text: str) -> bytes:
    return b"(" + _encode(text) + b")"


def build_pdf(page: Page, *, title: str) -> bytes:
    """A complete single-page A4 PDF with a compressed content stream."""
    stream = zlib.compress(page.content(), 9)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Title " + _pdf_string(title) + b" /Producer (Marceline) >>",
    ]  # fmt: skip
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(output)
    output += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R /Info 7 0 R >>\n"
        b"startxref\n" + str(xref).encode() + b"\n%%EOF\n"
    )  # fmt: skip
    return bytes(output)
