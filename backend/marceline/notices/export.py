"""What the customer takes away (issue #33, ADR 0035 amendment 2026-09-18).

The customer signs and sends the notice themselves, and the notice flow gives them three
affordances for that: copy the text, open it in their own mail program, download it. Copying and a
`mailto:` link already existed on the screen; this module is the third — a **file**. Nothing here sends anything: it turns text that is
already frozen on the draft into bytes a browser saves, and the one place Marceline may put a
notice into a mail transport stays `services.sandbox_send`.

Three formats, chosen from what the provider directory's channels actually need (ADR 0027
amendment: `catalog.ComplaintChannel.Kind`), not from what is technically possible:

- **`txt`** — the approved text, byte for byte. A web-form channel (`web_form`,
  `provider_copyright_policy…`) is filled by pasting, and a customer archiving the notice wants
  exactly the bytes whose sha256 the draft froze. It must stay byte-identical to
  `NoticeDraft.approved_text`, or the checksum in the audit trail stops meaning anything.
- **`eml`** — an unsent RFC 5322 message for an email channel (`abuse_email`, `dsa_art16`,
  `dmca_512c`, `dmca_512d`): `To` and `Subject` pre-filled, the approved text as the body, so the
  customer opens it in their own mail program and sends it from their own mailbox. It carries
  **no `From`, no `Date` and no `Message-ID`** on purpose — those are the sender's, the customer
  is the sender, and a Marceline address in a `From` header would be exactly the claim ADR 0035
  exists to avoid. `X-Unsent: 1` is what mail clients read as "this is a draft, open it for
  editing" rather than "this is a message that arrived". A `mailto:` link cannot do this: browsers
  and mail clients truncate long `mailto:` bodies, and a notice listing several URLs is long.
- **`annex`** — the evidence annex: one plain-text block per reported location with what the
  technical record holds, for a web form's "additional information" field and for the customer's
  own file. It repeats no legal assertion and **discloses no fingerprint value, band, distance or
  frame count** — the same rule the renderers follow (ADR 0035; no confidence insight for
  customers, `backend/marceline/CLAUDE.md`).

**No PDF.** Not one channel of the provider directory asks for one, and `billing/pdf.py`, the only
PDF writer in the repository (deliberately dependency-free), encodes to Windows-1252 — a URL or a
title outside that range would be written as `?` into a document the customer then sends. A
mangled reported URL is a worse failure than no PDF, so the record stays plain text; revisit only
if a real recipient channel asks for a signed document.

Pure: no Django, no database, no model import. `services` assembles the context from the draft and
logs the export; this module only formats.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from email.policy import SMTP

from matching.fingerprint import KIND_PHOTOS, KIND_VIDEO

#: The export formats, in the order the screen offers them.
FORMATS: tuple[str, ...] = ("txt", "eml", "annex")

TEXT_TYPE = "text/plain; charset=utf-8"
EML_TYPE = "message/rfc822"

#: Anything that could start a new header line, or end the filename, is not allowed through.
_CONTROL = re.compile(r"[\r\n\t\x00-\x1f\x7f]")
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class ExportFile:
    """One file to hand to the browser. `content` is already encoded."""

    filename: str
    content_type: str
    content: bytes


def _header_safe(value: str) -> str:
    """One line, no control characters: a header value can never open a second header.

    `Subject` is built from the draft's own work title and `To` from the recipient address the
    customer adopted — both customer-supplied strings that reach a file the customer's mail
    program will parse, so a `\\r\\n` in either would be header injection into their own outbox.
    """
    return _CONTROL.sub(" ", value).strip()


def _filename(notice_id: str, suffix: str) -> str:
    stem = _UNSAFE_FILENAME.sub("_", notice_id) or "notice"
    return f"{stem}{suffix}"


#: The file name suffix of each format. One table, because production serves the bytes from the
#: signed bundle (ADR 0046) and puts the `Content-Disposition` on the answer itself: the name a
#: customer's browser saves must not depend on which machine answered.
SUFFIXES: dict[str, str] = {"txt": ".txt", "eml": ".eml", "annex": "-annex.txt"}


def filename_for(notice_id: str, fmt: str) -> str:
    """The name of one download, on either machine."""
    return _filename(notice_id, SUFFIXES[fmt])


def text_file(notice_id: str, text: str) -> ExportFile:
    """The approved text itself, byte for byte — the file whose sha256 the draft froze."""
    return ExportFile(
        filename=filename_for(notice_id, "txt"),
        content_type=TEXT_TYPE,
        content=text.encode("utf-8"),
    )


def eml_bytes(*, subject: str, body: str, to: str) -> bytes:
    """An unsent message: `To`, `Subject`, the body, and deliberately nothing that names a sender.

    Built with the standard library's `email` package, so the result is a well-formed RFC 5322
    message every mail client opens. It is never handed to a transport here — building bytes is
    not sending, and `services.export_notice` is a read of the draft plus one audit row.
    """
    message = EmailMessage()
    recipient = _header_safe(to)
    if recipient:
        message["To"] = recipient
    message["Subject"] = _header_safe(subject)
    # What Outlook and several other clients read as "open this as a draft to edit and send",
    # rather than as a message that already arrived.
    message["X-Unsent"] = "1"
    message.set_content(body, subtype="plain", charset="utf-8")
    return message.as_bytes(policy=SMTP)


def eml_file(notice_id: str, *, subject: str, body: str, to: str) -> ExportFile:
    return ExportFile(
        filename=filename_for(notice_id, "eml"),
        content_type=EML_TYPE,
        content=eml_bytes(subject=subject, body=body, to=to),
    )


@dataclass(frozen=True)
class AnnexRow:
    """One reported location, as the technical record holds it."""

    url: str
    first_seen: date | None
    last_checked: datetime | None
    #: `matching.copy_extent.CopyExtent` value, snapshotted on the finding.
    extent: str
    #: True when the customer ticked the per-finding exception review; None when none was needed.
    exception_reviewed: bool | None
    #: The day a human confirmed this copy, snapshotted on the finding (issue #142). None only for a
    #: decision taken before that date was recorded, and then the row simply omits the line.
    reviewed_on: date | None = None


@dataclass(frozen=True)
class AnnexContext:
    language: str
    notice_id: str
    work_title: str
    work_id: str
    #: `video` or `photos` (`matching.Reference.kind`): the annex's basis sentence says what the
    #: reviewer actually compared, and for a photo collection that is not audiovisual content
    #: (issue #116, ADR 0053).
    work_kind: str
    legitimate_source_url: str
    recipient_name: str
    recipient_hostname: str
    approved_text_sha256: str
    rows: tuple[AnnexRow, ...]


#: What each `matching.copy_extent.CopyExtent` value may be *said* to mean in a document a
#: recipient reads. `full_copy` is the one that needed correcting (2026-09-19): "the whole reference work" is a claim
#: about the whole file, and the record behind it is a matched, in-order timeline spanning at least
#: `FULL_COPY_MIN_COVERAGE` of the original — sampled moments, corroborated by the page's own
#: duration where it has one. So the phrase now says what was compared. A duration-based
#: wording ("matches in running time") would be wrong about this classifier, which assigns
#: `full_copy` from timeline coverage and uses duration only to contradict it (`matching/copy_extent.py` rules 4 and 5).
_EXTENT_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "full_copy": "the reference work in full, on the moments compared",
        "excerpt": "part of the reference work",
        "compilation_candidate": "material that may combine several works",
        "unknown": "not determined from the record",
    },
    "de": {
        "full_copy": "das gesamte Referenzwerk, bezogen auf die verglichenen Zeitpunkte",
        "excerpt": "ein Teil des Referenzwerks",
        "compilation_candidate": "Material, das mehrere Werke zusammenfassen kann",
        "unknown": "aus dem Datenbestand nicht bestimmt",
    },
}


def _extent_words(extent: str, language: str) -> str:
    table = _EXTENT_WORDS[language]
    return table.get(extent, table["unknown"])


def _fmt_date(value: date | None) -> str:
    return value.isoformat() if value is not None else "—"


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value is not None else "—"


#: What the reviewer recorded having compared, per work kind and language — the lines of one annex
#: row, indented as the annex prints them.
#:
#: Scoped to the images examined (2026-09-19), in the same words as `renderers._evidence_paragraph`: the annex and the notice describe
#: **one** finding, and a recipient who reads them side by side must not find two different claims.
#: A reviewer sees the images the reported location itself publishes, against the reference work's
#: stored fingerprints — never the located file, and never a frame of the original, which we do not
#: store. The German photo line says *Fotografien*, not `Lichtbilder`: the annex uses the everyday
#: word and does not pick a legal category for the notifying party.
_BASIS_LINES: dict[str, dict[str, list[str]]] = {
    "de": {
        KIND_VIDEO: [
            "   Grundlage: wahrnehmungsbasierter Vergleich mit dem Referenzwerk; eine prüfende",
            "     Person hat den Vergleich geprüft und festgehalten, dass das an der gemeldeten",
            "     Fundstelle abrufbare Material auf den geprüften Bildern mit dem Referenzwerk",
            "     übereinstimmt.",
        ],
        KIND_PHOTOS: [
            "   Grundlage: wahrnehmungsbasierter Vergleich mit dem Referenzwerk; eine prüfende",
            "     Person hat den Vergleich geprüft und festgehalten, dass das an der gemeldeten",
            "     Fundstelle abrufbare Material auf den geprüften Bildern mit Fotografien des",
            "     Referenzwerks übereinstimmt; eine Fundstelle kann einzelne und nicht alle",
            "     Fotografien der Sammlung enthalten.",
        ],
    },
    "en": {
        KIND_VIDEO: [
            "   Basis: perceptual comparison with the reference work; a human reviewer reviewed",
            "     that comparison and recorded that the material available at the reported",
            "     location matches the reference work on the images examined.",
        ],
        KIND_PHOTOS: [
            "   Basis: perceptual comparison with the reference work; a human reviewer reviewed",
            "     that comparison and recorded that the material available at the reported",
            "     location matches photographs of the reference work on the images examined; a",
            "     location may carry some and not all photographs of the collection.",
        ],
    },
}


def _basis_lines(work_kind: str, language: str) -> list[str]:
    table = _BASIS_LINES[language]
    return table.get(work_kind, table[KIND_VIDEO])


def _annex_row(row: AnnexRow, index: int, language: str, work_kind: str) -> str:
    extent = _extent_words(row.extent, language)
    if language == "de":
        lines = [
            f"{index}. {row.url}",
            f"   Erstmals festgestellt: {_fmt_date(row.first_seen)}",
            f"   Zuletzt als abrufbar festgestellt: {_fmt_dt(row.last_checked)}",
            f"   Umfang der gemeldeten Kopie: {extent}",
            *_basis_lines(work_kind, "de"),
        ]
        # Per row, because the rows of one draft can have been reviewed on different days (issue
        # #142). Omitted rather than dashed when our record holds no date.
        if row.reviewed_on is not None:
            lines.append(f"   Von einer Person geprüft am: {row.reviewed_on.isoformat()}")
        if row.exception_reviewed is not None:
            answer = "ja" if row.exception_reviewed else "nein"
            lines.append(f"   Schrankenprüfung durch die meldende Partei erklärt: {answer}")
        return "\n".join(lines)
    lines = [
        f"{index}. {row.url}",
        f"   First detected: {_fmt_date(row.first_seen)}",
        f"   Last found available: {_fmt_dt(row.last_checked)}",
        f"   Extent of the reported copy: {extent}",
        *_basis_lines(work_kind, "en"),
    ]
    if row.reviewed_on is not None:
        lines.append(f"   Reviewed by a person on: {row.reviewed_on.isoformat()}")
    if row.exception_reviewed is not None:
        answer = "yes" if row.exception_reviewed else "no"
        lines.append(f"   Exception review declared by the notifying party: {answer}")
    return "\n".join(lines)


def annex_text(context: AnnexContext) -> str:
    """The evidence annex. Facts from our own record only — no legal conclusion, no hash of any
    picture, no band, no distance, no frame count, and no image of any kind."""
    rows = "\n\n".join(
        _annex_row(row, index, context.language, context.work_kind)
        for index, row in enumerate(context.rows, start=1)
    )
    if context.language == "de":
        return (
            "Anlage zum Nachweis der Identifizierung\n"
            f"Entwurfskennung: {context.notice_id}\n"
            f"Prüfsumme des freigegebenen Meldungstexts (SHA-256): "
            f"{context.approved_text_sha256 or '—'}\n\n"
            f"Werk: „{context.work_title}“ ({context.work_id})\n"
            f"Rechtmäßig erhältlich unter: {context.legitimate_source_url or '[Quelle]'}\n"
            f"Empfänger der Meldung: {context.recipient_name} "
            f"({context.recipient_hostname})\n\n"
            "Diese Anlage gehört zu der oben bezeichneten Meldung und listet je gemeldeter "
            "Fundstelle auf, was der technische Datenbestand enthält. Sie enthält keine "
            "rechtliche Bewertung; diese trifft ausschließlich die meldende Partei in der Meldung "
            "selbst. Bilder, Einzelbilder, Vorschaubilder oder Kopien des gemeldeten Materials "
            "sind nicht beigefügt, und es werden keine Fingerabdruckwerte mitgeteilt.\n\n"
            f"{rows}\n"
        )
    return (
        "Annex: evidence of identification\n"
        f"Draft reference: {context.notice_id}\n"
        f"Checksum of the approved notice text (SHA-256): {context.approved_text_sha256 or '—'}\n\n"
        f'Work: "{context.work_title}" ({context.work_id})\n'
        f"Legitimately available at: {context.legitimate_source_url or '[source]'}\n"
        f"Notice recipient: {context.recipient_name} ({context.recipient_hostname})\n\n"
        "This annex belongs to the notice named above and lists, for each reported location, what "
        "the technical record holds. It makes no legal assessment; the notifying party makes "
        "those in the notice itself. No image, frame, thumbnail or copy of the reported material "
        "is attached, and no fingerprint value is disclosed.\n\n"
        f"{rows}\n"
    )


def annex_file(notice_id: str, context: AnnexContext) -> ExportFile:
    return ExportFile(
        filename=filename_for(notice_id, "annex"),
        content_type=TEXT_TYPE,
        content=annex_text(context).encode("utf-8"),
    )
