"""Golden texts for the four renderers (issue #33, ADR 0035), DE and EN.

Not exact byte-for-byte prose review (that is counsel's job, docs/legal/counsel/), but the
structural invariants that are load-bearing: never an image/hash/band, the
customer's own legal paragraph appears, the statutory declaration for the variant appears, and the
DMCA variants are always rendered in English even when the draft's working language is German.
"""

from datetime import UTC, date, datetime

import pytest

from marceline.notices.renderers import (
    DMCA_DECLARATION_EN,
    DSA_DECLARATION,
    NoticeContext,
    NoticeFindingFact,
    render,
)

FORBIDDEN_SUBSTRINGS = ("PDQ", "Hamming", "confidence", "band", "distance")


def context(**overrides: object) -> NoticeContext:
    values: dict[str, object] = dict(
        language="en",
        claimant_name="Alpha Studio",
        claimant_capacity="owner",
        underlying_owner_name="",
        claimant_email="rights@alpha.example",
        claimant_address="Musterstrasse 1, 10115 Berlin, Germany",
        claimant_phone="+49 30 1234567",
        work_title="Sunset Scene",
        work_id="orig_deadbeefdeadbeefdeadbeefdeadbeef",
        work_kind="video",
        legitimate_source_url="https://alpha.example/store/sunset-scene",
        publication_date=date(2026, 1, 15),
        findings=(
            NoticeFindingFact(
                url="https://hoster.example/watch/123",
                first_seen=date(2026, 9, 1),
                last_checked=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
            ),
        ),
        recipient_name="Example Hosting",
        recipient_email="abuse@hosting.example",
        recipient_url="",
        legal_paragraph="I confirm my rights and have considered exceptions.",
    )
    values.update(overrides)
    return NoticeContext(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("variant", ["dsa_art16", "dmca_512c", "dmca_512d", "provider_policy"])
@pytest.mark.parametrize("language", ["en", "de"])
def test_never_carries_imagery_hashes_bands_or_distances(variant: str, language: str) -> None:
    text = render(variant, context(language=language))

    assert "http" in text  # the URL is present as text
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in text
    assert "screenshot" not in text.lower()
    assert "thumbnail" not in text.lower() or "no image, frame, thumbnail" in text.lower()


@pytest.mark.parametrize("variant", ["dsa_art16", "dmca_512c", "dmca_512d", "provider_policy"])
def test_carries_the_customers_own_legal_paragraph(variant: str) -> None:
    text = render(variant, context(legal_paragraph="UNIQUE-LEGAL-MARKER-42"))

    assert "UNIQUE-LEGAL-MARKER-42" in text


@pytest.mark.parametrize("language", ["en", "de"])
def test_dsa_art16_carries_the_dsa_declaration(language: str) -> None:
    text = render("dsa_art16", context(language=language))

    assert DSA_DECLARATION[language] in text
    assert ("Art. 16" if language == "de" else "Article 16") in text


@pytest.mark.parametrize("language", ["en", "de"])
def test_provider_policy_never_claims_a_statute(language: str) -> None:
    text = render("provider_policy", context(language=language))

    assert "Art. 16" not in text
    assert "Article 16" not in text
    assert "512" not in text


@pytest.mark.parametrize("draft_language", ["en", "de"])
def test_dmca_variants_are_always_english(draft_language: str) -> None:
    text_c = render("dmca_512c", context(language=draft_language))
    text_d = render("dmca_512d", context(language=draft_language))

    assert DMCA_DECLARATION_EN in text_c
    assert DMCA_DECLARATION_EN in text_d
    assert "17 U.S.C. § 512(c)" in text_c
    assert "17 U.S.C. § 512(d)" in text_d
    # No German-only wording leaked in from the draft's own language.
    assert "Meldung" not in text_c
    assert "Meldung" not in text_d


def test_dmca_512d_frames_the_material_as_a_reference_or_link() -> None:
    text = render("dmca_512d", context())

    assert "reference or link" in text.lower()


def test_dmca_512d_says_which_references_are_to_be_disabled() -> None:
    """§ 512(d)(3) substitutes for § 512(c)(3)(A)(iii) "identification of the reference or link …
    and information reasonably sufficient to permit the service provider to locate that reference
    or link". The URLs are the material's locations, so the notice has to say that the links to be
    disabled are the ones pointing at them."""
    text = render("dmca_512d", context())
    assert "references or links to be disabled" in text
    assert "references or links to be disabled" not in render("dmca_512c", context())


def test_a_dmca_contact_block_prints_only_the_fields_the_claimant_gave() -> None:
    """Address and telephone are optional on the customer's side; § 512(c)(3)(A)(iv) asks for
    contact information "reasonably sufficient" and names them as examples. A line of separators
    around an e-mail looks like a notice with its fields stripped out, which is a reason for a
    designated agent to reject it."""
    text = render("dmca_512c", context(claimant_address="", claimant_phone=""))

    assert "rights@alpha.example" in text
    assert " ·  · " not in text
    assert "\n · " not in text


def test_multiple_findings_all_appear() -> None:
    two = context(
        findings=(
            NoticeFindingFact(url="https://hoster.example/a", first_seen=None, last_checked=None),
            NoticeFindingFact(url="https://hoster.example/b", first_seen=None, last_checked=None),
        )
    )

    text = render("dsa_art16", two)

    assert "https://hoster.example/a" in text
    assert "https://hoster.example/b" in text


# --- Photo collections (issue #116, ADR 0053) ---------------------------------------------------


@pytest.mark.parametrize("variant", ["dsa_art16", "dmca_512c", "dmca_512d", "provider_policy"])
def test_a_photo_collection_is_never_described_as_audiovisual(variant: str) -> None:
    text = render(variant, context(work_kind="photos"))

    assert "audiovisual" not in text.lower()
    assert "photograph" in text.lower()


def test_the_german_photo_paragraph_decides_no_classification() -> None:
    # `Lichtbild` (§ 72 UrhG) and `Lichtbildwerk` (§ 2 Abs. 1 Nr. 5 UrhG) are different rights with
    # different terms; the notice must not pick one for the notifying party.
    text = render("dsa_art16", context(language="de", work_kind="photos"))

    assert "Fotografien" in text
    assert "Lichtbild" not in text
    assert "audiovisuell" not in text.lower()


def test_the_photo_paragraph_says_a_location_may_carry_part_of_the_set() -> None:
    # A collection is a collection: a location that reposts four pictures of forty is the normal
    # case, and the paragraph — not only the annex's extent line — has to say so.
    assert "some and not all photographs" in render("dsa_art16", context(work_kind="photos"))
    assert "nicht alle Fotografien" in render(
        "dsa_art16", context(language="de", work_kind="photos")
    )


@pytest.mark.parametrize("variant", ["dsa_art16", "dmca_512c", "dmca_512d", "provider_policy"])
@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("work_kind", ["video", "photos"])
def test_the_reviewers_finding_is_scoped_and_never_called_a_confirmation(
    variant: str, language: str, work_kind: str
) -> None:
    """What a reviewer sees is the images the reported location publishes, against stored
    fingerprints — never the located file, and never a frame of the original, which we do not
    store. So the sentence says "reviewed … and recorded", scoped to those images, and the annex
    says the same thing (`notices/export.py _BASIS_LINES`)."""
    text = render(variant, context(language=language, work_kind=work_kind))

    assert "reviewed that comparison and recorded" in text or (
        "den Vergleich geprüft und festgehalten" in text
    )
    assert "images examined" in text or "geprüften Bildern" in text
    assert "reviewer confirmed" not in text
    assert "prüfende Person hat bestätigt" not in text


@pytest.mark.parametrize("variant", ["dsa_art16", "dmca_512c", "dmca_512d", "provider_policy"])
@pytest.mark.parametrize("language", ["en", "de"])
def test_no_placeholder_reaches_a_recipient_except_the_customers_own_signature_fields(
    variant: str, language: str
) -> None:
    """A rendered draft is a document the customer sends. Every bracket in it is either theirs to
    fill when they sign (place, date) or a bug: the evidence paragraph shipped with a literal
    `On [review date],` that nothing ever substituted, in a sentence stating what happened on that
    date (the review date returns when a draft snapshots it)."""
    text = render(variant, context(language=language))

    allowed = ("[Place, date]", "[Ort, Datum]", "[date]", f"[{context().claimant_name}]")
    for chunk in allowed:
        text = text.replace(chunk, "")
    assert "[" not in text, text


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_photo_paragraph_keeps_every_limit_the_video_one_has(language: str) -> None:
    text = render("dsa_art16", context(language=language, work_kind="photos"))

    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in text


def test_an_unknown_work_kind_reads_as_the_video_wording() -> None:
    # `_work_kind` falls back to `video` where no reference is in reach; a notice never renders a
    # sentence about a shape of work nobody named.
    assert render("dsa_art16", context(work_kind="video")) == render("dsa_art16", context())


# --- the human-review date (issue #142) -----------------------------------------------------------

REVIEWED = date(2026, 9, 10)


@pytest.mark.parametrize("variant", ["dsa_art16", "dmca_512c", "dmca_512d", "provider_policy"])
@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("work_kind", ["video", "photos"])
def test_the_review_date_is_named_in_the_evidence_paragraph(
    variant: str, language: str, work_kind: str
) -> None:
    """The review date is one of the minimum evidence fields; a placeholder once stood where it
    belonged. It is back, from the finding's own snapshot."""
    text = render(
        variant,
        context(
            language=language,
            work_kind=work_kind,
            findings=(
                NoticeFindingFact(
                    url="https://hoster.example/watch/123",
                    first_seen=date(2026, 9, 1),
                    last_checked=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
                    reviewed_on=REVIEWED,
                ),
            ),
        ),
    )

    assert REVIEWED.isoformat() in text
    # In the sentence it belongs in, not appended somewhere: it sits inside the reviewer clause.
    assert (
        f"reviewed that comparison on {REVIEWED.isoformat()} and recorded" in text
        or f"den Vergleich am {REVIEWED.isoformat()} geprüft und festgehalten" in text
    )


@pytest.mark.parametrize("language", ["en", "de"])
def test_locations_reviewed_on_different_days_read_as_a_range(language: str) -> None:
    """A single date would be false for every row but one; the annex prints each row's own."""
    text = render(
        "dsa_art16",
        context(
            language=language,
            findings=(
                NoticeFindingFact(
                    url="https://hoster.example/a",
                    first_seen=None,
                    last_checked=None,
                    reviewed_on=date(2026, 9, 2),
                ),
                NoticeFindingFact(
                    url="https://hoster.example/b",
                    first_seen=None,
                    last_checked=None,
                    reviewed_on=REVIEWED,
                ),
            ),
        ),
    )

    assert "2026-09-02" in text and REVIEWED.isoformat() in text
    assert "between 2026-09-02 and" in text or "zwischen dem 2026-09-02 und dem" in text


@pytest.mark.parametrize("language", ["en", "de"])
def test_a_finding_with_no_recorded_date_leaves_the_sentence_whole(language: str) -> None:
    """Never a placeholder and never a dash: a draft from before the date was recorded simply does
    not name one."""
    text = render("dsa_art16", context(language=language))

    assert "[review date]" not in text and "[Prüfdatum]" not in text
    assert (
        "reviewed that comparison and recorded" in text
        or "den Vergleich geprüft und festgehalten" in text
    )
