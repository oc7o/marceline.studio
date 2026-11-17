"""Route-specific renderers (issue #33, ADR 0035): plain text, never images, hashes or a band.

One canonical fact model, four renderers — `dsa_art16`, `dmca_512c`, `dmca_512d`,
`provider_policy` — following docs/legal/answers/01-takedown-notices.md Part Two "(c) Notice
content" and the "Version-one implementation specification". Every renderer:

- states **content identity as a technical finding**, never "infringement" or "verified leak";
- carries the customer's own declarations, never Marceline's;
- attaches **no image, frame, thumbnail or PDQ hash** ("Minimum evidence fields for the template",
  Part Two "(d)");
- ends with the legally material paragraph the customer supplied and the statutory declaration
  the chosen variant needs.

An original is a video or a photo collection (ADR 0020), and since issue #116 a customer can add
either in the app (ADR 0053). Only the evidence paragraph knows the difference: everything else in
a notice is about locations, the claimant and declarations, none of which depends on the shape of
the work. Both evidence paragraphs are drafts to be reviewed before real use (see
`_photo_evidence_paragraph`).

Three things changed on 2026-09-19:

- **no unfilled placeholder** may reach a recipient. The paragraph used to open with a literal
  `On [review date],` / `Am [Prüfdatum]` that nothing ever substituted, in a sentence that then
  states what happened on that date. The date is our record, not the customer's to invent, so the
  opening is gone until a review date is snapshotted on the draft (issue: restore it then);
- the **reviewer sentence is scoped to what a reviewer can see**: the images published at the
  reported location, compared against the reference work's stored fingerprints. It never says the
  reviewer "confirmed" content identity — the annex already said "reviewed … and recorded", and
  the two documents must not describe one finding differently;
- the § 512(d) variant names the **reference or link** to be disabled, which is the element
  § 512(d)(3) substitutes for § 512(c)(3)(A)(iii).

DSA Art. 16 and the provider-policy channel are rendered in the customer's chosen language, because
either can go to an EU or a non-EU provider that may accept German or English. The two DMCA
variants are always rendered in English — the statutory notice a US recipient expects — even when
the customer's working language is German; the screen may show German labels around it, but the
notice body itself is not translated (a US host expects the statutory §512 wording, not a
translation of it).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from marceline.notices.models import RenderedVariant
from matching.fingerprint import KIND_PHOTOS

DSA_DECLARATION = {
    "en": (
        "I confirm that I have a bona fide belief that the information and allegations "
        "contained in this notice are accurate and complete. I consider the information "
        "identified above to be unlawful for the reasons stated in this notice."
    ),
    "de": (
        "Ich bestätige, dass ich in gutem Glauben davon überzeugt bin, dass die in dieser "
        "Meldung enthaltenen Angaben und Behauptungen richtig und vollständig sind. Ich halte "
        "die oben bezeichneten Informationen aus den in dieser Meldung genannten Gründen für "
        "rechtswidrig."
    ),
}

DMCA_DECLARATION_EN = (
    "I have a good-faith belief that use of the material in the manner complained of is not "
    "authorised by the copyright owner, its agent, or the law. The information in this "
    "notification is accurate and, under penalty of perjury, I am the owner of the exclusive "
    "right allegedly infringed or am authorised to act on behalf of the owner of that exclusive "
    "right."
)


@dataclass(frozen=True)
class NoticeFindingFact:
    url: str
    first_seen: date | None
    last_checked: datetime | None
    #: The day a human confirmed this copy, snapshotted on the finding (issue #142). None only for a
    #: decision taken before that date was recorded — then the paragraph simply does not name one,
    #: because the alternative was the literal `[review date]` a customer used to be able to send.
    reviewed_on: date | None = None


@dataclass(frozen=True)
class NoticeContext:
    """Everything a renderer needs. Text only — no hash, band, distance or frame count."""

    language: str
    claimant_name: str
    claimant_capacity: str  # "owner" | "authorised_agent"
    underlying_owner_name: str
    claimant_email: str
    claimant_address: str
    claimant_phone: str
    work_title: str
    work_id: str
    #: `video` or `photos` (`matching.Reference.kind`, ADR 0020). A notice whose evidence paragraph
    #: misdescribes the work is worse than no notice, so the shape of the work is a fact the
    #: renderer is given rather than one it assumes (issue #116, ADR 0053).
    work_kind: str
    legitimate_source_url: str
    publication_date: date | None
    findings: tuple[NoticeFindingFact, ...]
    recipient_name: str
    recipient_email: str
    recipient_url: str
    legal_paragraph: str = field(
        default="",
        metadata={"doc": "The customer's own rights/authorisation/exceptions statement."},
    )


def _capacity_text(capacity: str, underlying_owner_name: str, language: str) -> str:
    if capacity == "authorised_agent":
        return (
            f"authorised to act for {underlying_owner_name}"
            if language == "en"
            else f"befugt, für {underlying_owner_name} zu handeln"
        )
    return (
        "the owner of the exclusive right"
        if language == "en"
        else "Inhaber des ausschließlichen Rechts"
    )


def _fmt_date(value: date | None) -> str:
    return value.isoformat() if value is not None else "—"


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value is not None else "—"


def _review_phrase(context: NoticeContext) -> str:
    """The review date as a phrase — `on 2026-09-19`, `between … and …`, or nothing at all.

    The leading space belongs to the phrase, so an empty one leaves the sentence intact.

    The human-review date answer 01 lists among the minimum evidence fields, back in the sentence
    it belongs in (issue #142). Three shapes on purpose:

    - one date, because a draft usually reports one location or a batch reviewed the same day;
    - a range when the reported locations were reviewed on different days, since "on D" would then
      be false for all but one of them and the annex prints each row's own date anyway;
    - **nothing at all** when our record holds no date — never a placeholder. The literal
      `[review date]` must never reach a customer's download, and
      `test_notices_renderers.py` refuses any bracket in a rendered draft.
    """
    dates = sorted({row.reviewed_on for row in context.findings if row.reviewed_on is not None})
    if not dates:
        return ""
    if context.language == "de":
        if len(dates) == 1:
            return f" am {dates[0].isoformat()}"
        return f" zwischen dem {dates[0].isoformat()} und dem {dates[-1].isoformat()}"
    if len(dates) == 1:
        return f" on {dates[0].isoformat()}"
    return f" between {dates[0].isoformat()} and {dates[-1].isoformat()}"


def _photo_evidence_paragraph(
    context: NoticeContext, urls: str, first: date | None, checked: datetime | None
) -> str:
    """The evidence paragraph for a photo collection (issue #116, ADR 0053).

    **Draft wording.** The video paragraph below comes from
    `docs/legal/answers/01-takedown-notices.md` Part Two "(c)"; this one follows its structure and
    its limits — a technical finding, never "infringement", no image, no hash, no band, no frame
    count. Two things changed on 2026-09-19:

    - the German text says *Fotografien*, **not** `Lichtbilder`. § 2 Abs. 1 Nr. 5 UrhG
      (*Lichtbildwerk*) and § 72 UrhG (*Lichtbild*, 50 years from publication) are different
      rights with different terms, and a notice must not decide a classification the notifying
      party was never asked about;
    - the reviewer sentence is scoped to the images examined and says outright that a location may
      carry some and not all images of the collection — a set is a collection, and a partial match
      is the normal case.
    """
    review = _review_phrase(context)
    if context.language == "de":
        return (
            f"Das unter {urls} abrufbare Bildmaterial wurde mit dem registrierten "
            f"Referenzwerk „{context.work_title}“ ({context.work_id}) verglichen, einer Sammlung "
            f"von Fotografien, die rechtmäßig unter "
            f"{context.legitimate_source_url or '[Quelle]'} erhältlich ist. Das "
            "wahrnehmungsbasierte Vergleichssystem von Marceline hat übereinstimmende Bilder "
            f"erkannt, und eine prüfende Person hat den Vergleich{review} geprüft und "
            "festgehalten, dass das an der gemeldeten Fundstelle abrufbare Material auf den "
            "geprüften Bildern mit "
            "Fotografien des Referenzwerks übereinstimmt; eine Fundstelle kann einzelne und nicht "
            "alle Fotografien der Sammlung enthalten. Die Fundstelle wurde erstmals am "
            f"{_fmt_date(first)} festgestellt und zuletzt am {_fmt_dt(checked)} als abrufbar "
            "bestätigt. Diese Angabe betrifft ausschließlich die inhaltliche Übereinstimmung; die "
            "meldende Partei bestätigt ihre Inhaberschaft oder Befugnis, das Fehlen einer "
            "Erlaubnis und die für diese Meldung geltenden Erklärungen gesondert. Bilder, "
            "Vorschaubilder oder Kopien des gemeldeten Materials sind nicht beigefügt."
        )
    return (
        f"The photographic material available at {urls} was compared with the "
        f'registered reference work "{context.work_title}" ({context.work_id}), a collection of '
        f"photographs which is legitimately available at "
        f"{context.legitimate_source_url or '[source]'}. Marceline's perceptual-comparison system "
        f"identified matching images, and a human reviewer reviewed that comparison{review} and "
        "recorded that the material available at the reported location matches photographs of the "
        "reference work on the images examined; a reported location may carry some and not all "
        f"photographs of the collection. The location was first detected on "
        f"{_fmt_date(first)} and was last confirmed available on {_fmt_dt(checked)}. This "
        "statement concerns content identity only; the complaining party separately confirms "
        "ownership or authority, lack of authorisation and the declarations applicable to this "
        "notice. No image, thumbnail or copy of the reported material is attached."
    )


def _evidence_paragraph(context: NoticeContext) -> str:
    checked = max(
        (row.last_checked for row in context.findings if row.last_checked is not None),
        default=None,
    )
    first = min(
        (row.first_seen for row in context.findings if row.first_seen is not None), default=None
    )
    urls = "; ".join(row.url for row in context.findings)
    if context.work_kind == KIND_PHOTOS:
        return _photo_evidence_paragraph(context, urls, first, checked)
    review = _review_phrase(context)
    if context.language == "de":
        return (
            f"Das unter {urls} abrufbare audiovisuelle Material wurde mit dem "
            f"registrierten Referenzwerk „{context.work_title}“ ({context.work_id}) verglichen, "
            f"das rechtmäßig unter {context.legitimate_source_url or '[Quelle]'} erhältlich ist. "
            "Das wahrnehmungsbasierte Vergleichssystem von Marceline hat übereinstimmende "
            f"audiovisuelle Inhalte erkannt, und eine prüfende Person hat den Vergleich{review} "
            "geprüft und festgehalten, dass das an der gemeldeten Fundstelle abrufbare Material "
            "auf den geprüften Bildern mit dem Referenzwerk übereinstimmt. Die "
            f"Fundstelle wurde erstmals am {_fmt_date(first)} festgestellt und zuletzt am "
            f"{_fmt_dt(checked)} als abrufbar bestätigt. Diese Angabe betrifft ausschließlich die "
            "inhaltliche Übereinstimmung; die meldende Partei bestätigt ihre Inhaberschaft oder "
            "Befugnis, das Fehlen einer Erlaubnis und die für diese Meldung geltenden Erklärungen "
            "gesondert. Bilder, Einzelbilder, Vorschaubilder oder Kopien des gemeldeten Materials "
            "sind nicht beigefügt."
        )
    return (
        f"The audiovisual material available at {urls} was compared with the "
        f'registered reference work "{context.work_title}" ({context.work_id}), which is '
        f"legitimately available at {context.legitimate_source_url or '[source]'}. Marceline's "
        "perceptual-comparison system identified matching audiovisual content, and a human "
        f"reviewer reviewed that comparison{review} and recorded that the material available at "
        "the reported location matches the reference work on the images examined. The location was "
        f"first detected on {_fmt_date(first)} and was last "
        f"confirmed available on {_fmt_dt(checked)}. This statement concerns content identity "
        "only; the complaining party separately confirms ownership or authority, lack of "
        "authorisation and the declarations applicable to this notice. No image, frame, "
        "thumbnail or copy of the reported material is attached."
    )


def _urls_block(context: NoticeContext, language: str) -> str:
    heading = (
        "Genaue elektronische Fundstellen" if language == "de" else "Exact electronic location(s)"
    )
    lines = "\n".join(f"  {row.url}" for row in context.findings)
    return f"{heading}\n{lines}"


def _claimant_block(context: NoticeContext, language: str) -> str:
    capacity = _capacity_text(context.claimant_capacity, context.underlying_owner_name, language)
    if language == "de":
        return (
            f"Name: {context.claimant_name}\n"
            f"E-Mail: {context.claimant_email}\n"
            f"Eigenschaft: {capacity}"
        )
    return f"Name: {context.claimant_name}\nEmail: {context.claimant_email}\nCapacity: {capacity}"


def _hosting_style_body(
    context: NoticeContext, *, subject_heading: str, recipient_heading: str
) -> str:
    """The shared DSA-style structure used by both `dsa_art16` and `provider_policy`.

    `subject_heading` and `recipient_heading` are the two strings that differ between a
    statutory Art. 16 notice and a provider's own copyright channel; everything else — the
    numbered sections, the evidence paragraph, the DSA-style accuracy/completeness declaration —
    is identical ("provider copyright policy" reuses the hosting-notice shape, it is not a
    DMCA-style perjury declaration).
    """
    language = context.language
    if language == "de":
        return (
            f"Betreff: {subject_heading} - urheberrechtlich geschütztes Werk "
            f'"{context.work_title}"\n\n'
            f"An: {context.recipient_name} - {recipient_heading} "
            f"{context.recipient_url or context.recipient_email}\n\n"
            "1. Meldende Person oder Einrichtung\n"
            f"{_claimant_block(context, 'de')}\n\n"
            f"2. {_urls_block(context, 'de')}\n\n"
            "3. Betroffenes Werk\n"
            f"„{context.work_title}“ ({context.work_id})"
            + (
                f", veröffentlicht am {_fmt_date(context.publication_date)}"
                if context.publication_date
                else ""
            )
            + f", rechtmäßig erhältlich unter {context.legitimate_source_url or '[Quelle]'}\n\n"
            "4. Begründung\n"
            f"{context.legal_paragraph}\n\n"
            "5. Nachweis der Identifizierung\n"
            f"{_evidence_paragraph(context)}\n\n"
            "6. Erklärungen\n"
            f"{DSA_DECLARATION['de']}\n\n"
            f"[Ort, Datum]\n[{context.claimant_name}]"
        )
    return (
        f'Subject: {subject_heading} — copyright-protected work "{context.work_title}"\n\n'
        f"To: {context.recipient_name} — {recipient_heading} "
        f"{context.recipient_url or context.recipient_email}\n\n"
        "1. Notifying individual or entity\n"
        f"{_claimant_block(context, 'en')}\n\n"
        f"2. {_urls_block(context, 'en')}\n\n"
        "3. Work concerned\n"
        f'"{context.work_title}" ({context.work_id})'
        + (f", published {_fmt_date(context.publication_date)}" if context.publication_date else "")
        + f", legitimately available at {context.legitimate_source_url or '[source]'}\n\n"
        "4. Reasoning\n"
        f"{context.legal_paragraph}\n\n"
        "5. Evidence of identification\n"
        f"{_evidence_paragraph(context)}\n\n"
        "6. Statements\n"
        f"{DSA_DECLARATION['en']}\n\n"
        f"[Place, date]\n[{context.claimant_name}]"
    )


#: How each renderer opens its first line. `split_subject` reads the subject back out of a
#: rendered notice, so the export can put it in an email header instead of repeating it in the
#: body. A new renderer either starts with one of these or gets no subject at all.
SUBJECT_PREFIXES = ("Subject: ", "Betreff: ")


def split_subject(text: str) -> tuple[str, str]:
    """A rendered notice split into its own subject line and the body below it.

    Every renderer's first line is `Subject: …` / `Betreff: …` followed by a blank line; the
    `.eml` export moves that line into the `Subject` header so the customer's mail program shows
    it where a mail program shows a subject. Returns `("", text)` unchanged for anything that does
    not start with a subject line, so the body is never silently truncated.
    """
    head, _, rest = text.partition("\n")
    for prefix in SUBJECT_PREFIXES:
        if head.startswith(prefix):
            return head[len(prefix) :].strip(), rest.lstrip("\n")
    return "", text


def render_dsa_art16(context: NoticeContext) -> str:
    heading = (
        "Meldung nach Art. 16 DSA" if context.language == "de" else "Notice under Article 16 DSA"
    )
    recipient = (
        "Melde- und Abhilfeverfahren nach Art. 16 DSA"
        if context.language == "de"
        else "Article 16 DSA notice-and-action mechanism"
    )
    return _hosting_style_body(context, subject_heading=heading, recipient_heading=recipient)


def render_provider_policy(context: NoticeContext) -> str:
    """A provider's own copyright/abuse channel: same shape, no statutory heading (answer 01's
    "Provider copyright channel — DMCA-style" caution: never claim a statute applies here)."""
    heading = (
        "Meldung über den Urheberrechtskanal des Anbieters"
        if context.language == "de"
        else "Notice via the provider's copyright channel"
    )
    recipient = (
        "Urheberrechtskanal des Anbieters"
        if context.language == "de"
        else "provider copyright/abuse channel"
    )
    return _hosting_style_body(context, subject_heading=heading, recipient_heading=recipient)


def _contact_line(context: NoticeContext) -> str:
    """Address, telephone and e-mail, and only the ones the claimant actually gave.

    § 512(c)(3)(A)(iv) asks for "information reasonably sufficient to permit the service provider
    to contact the complaining party, such as an address, telephone number, and, if available, an
    electronic mail address". Address and telephone are optional fields on the customer's side, so
    joining them unconditionally printed a line of separators around an e-mail
    (`" ·  · rights@…"`) — which reads like a notice with fields stripped out of it. One missing
    field is a question a designated agent may ask; three dots where a field should be is a reason
    to reject the whole notice (`answers/16-notice-template-wording.md`, § 512(c)(3)(A) element
    table).
    """
    parts = [context.claimant_address, context.claimant_phone, context.claimant_email]
    return " · ".join(part for part in parts if part.strip())


def _dmca_body(context: NoticeContext, *, delisting: bool) -> str:
    capacity = _capacity_text(context.claimant_capacity, context.underlying_owner_name, "en")
    material_heading = (
        "Reference or link claimed to be infringing and its location"
        if delisting
        else "Material claimed to be infringing and its location"
    )
    subject = (
        f"Subject: DMCA notification under 17 U.S.C. § 512(d) (search delisting) — "
        f'"{context.work_title}"'
        if delisting
        else f'Subject: DMCA notification under 17 U.S.C. § 512(c)(3) — "{context.work_title}"'
    )
    return (
        f"{subject}\n\n"
        f"To: {context.recipient_name} — designated agent "
        f"{context.recipient_url or context.recipient_email}\n\n"
        "1. Copyrighted work\n"
        f'"{context.work_title}" ({context.work_id})'
        + (
            f", first published {_fmt_date(context.publication_date)}"
            if context.publication_date
            else ""
        )
        + f", legitimately available at {context.legitimate_source_url or '[source]'}\n\n"
        f"2. {material_heading}\n"
        + "\n".join(f"  {row.url}" for row in context.findings)
        + (
            "\n  The references or links to be disabled are the ones in your service that point "
            "to the locations listed above."
            if delisting
            else ""
        )
        + "\n\n"
        "3. Evidence of identification\n"
        f"{_evidence_paragraph(context)}\n\n"
        "4. Complaining party\n"
        f"{context.claimant_name}, {capacity}\n"
        f"{_contact_line(context)}\n\n"
        "5. Statements\n"
        f"{context.legal_paragraph}\n\n"
        f"{DMCA_DECLARATION_EN}\n\n"
        "6. Signature\n"
        f"/{context.claimant_name}/   [date]"
    )


def render_dmca_512c(context: NoticeContext) -> str:
    return _dmca_body(context, delisting=False)


def render_dmca_512d(context: NoticeContext) -> str:
    return _dmca_body(context, delisting=True)


RENDERERS: dict[str, Callable[[NoticeContext], str]] = {
    str(RenderedVariant.DSA_ART16): render_dsa_art16,
    str(RenderedVariant.DMCA_512C): render_dmca_512c,
    str(RenderedVariant.DMCA_512D): render_dmca_512d,
    str(RenderedVariant.PROVIDER_POLICY): render_provider_policy,
}


def render(variant: str, context: NoticeContext) -> str:
    """The rendered notice text for one variant. DMCA variants are always English (see module
    docstring); `context.language` for them only affects nothing in the body."""
    if variant in (RenderedVariant.DMCA_512C, RenderedVariant.DMCA_512D):
        context = _english(context)
    return RENDERERS[variant](context)


def _english(context: NoticeContext) -> NoticeContext:
    if context.language == "en":
        return context
    return NoticeContext(
        **{**context.__dict__, "language": "en"},
    )
