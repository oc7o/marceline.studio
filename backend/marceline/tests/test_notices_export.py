"""The export builders (issue #33, ADR 0035 amendment 2026-09-18): pure, and they send nothing.

Three invariants carry the feature: the `txt` export is byte-identical to the approved text (its
sha256 is what the audit trail names), the `.eml` is an unsent message that names no sender, and
the annex states facts from our own record without a hash, a band or a distance.
"""

from datetime import UTC, date, datetime
from email import message_from_bytes
from email.policy import SMTP

import pytest

from marceline.notices.export import (
    FORMATS,
    AnnexContext,
    AnnexRow,
    annex_file,
    annex_text,
    eml_bytes,
    eml_file,
    text_file,
)
from marceline.notices.renderers import render, split_subject
from marceline.tests.test_notices_renderers import context as render_context

NOTICE_ID = "notice_00112233445566778899aabbccddeeff"


def annex_context(**overrides: object) -> AnnexContext:
    values: dict[str, object] = dict(
        language="en",
        notice_id=NOTICE_ID,
        work_title="Sunset Scene",
        work_id="orig_deadbeefdeadbeefdeadbeefdeadbeef",
        work_kind="video",
        legitimate_source_url="https://alpha.example/store/sunset-scene",
        recipient_name="Example Hosting",
        recipient_hostname="hoster.example",
        approved_text_sha256="a" * 64,
        rows=(
            AnnexRow(
                url="https://hoster.example/watch/123",
                first_seen=date(2026, 9, 1),
                last_checked=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
                extent="full_copy",
                exception_reviewed=None,
            ),
        ),
    )
    values.update(overrides)
    return AnnexContext(**values)  # type: ignore[arg-type]


def test_formats_are_the_three_the_channels_need() -> None:
    assert FORMATS == ("txt", "eml", "annex")


def test_text_export_is_byte_identical_to_the_approved_text() -> None:
    text = render("dsa_art16", render_context())

    file = text_file(NOTICE_ID, text)

    assert file.content == text.encode("utf-8")
    assert file.filename == f"{NOTICE_ID}.txt"
    assert file.content_type == "text/plain; charset=utf-8"


@pytest.mark.parametrize("variant", ["dsa_art16", "dmca_512c", "dmca_512d", "provider_policy"])
@pytest.mark.parametrize("language", ["en", "de"])
def test_every_rendered_notice_yields_a_subject_and_loses_nothing(
    variant: str, language: str
) -> None:
    text = render(variant, render_context(language=language))

    subject, body = split_subject(text)

    assert subject
    assert "\n" not in subject
    # Nothing but the subject line and the blank line after it is dropped.
    assert text.endswith(body)
    assert text.startswith(("Subject: ", "Betreff: "))


def test_split_subject_leaves_text_without_one_alone() -> None:
    assert split_subject("no subject here\n\nbody") == ("", "no subject here\n\nbody")


def test_eml_is_an_unsent_message_that_names_no_sender() -> None:
    text = render("dsa_art16", render_context())
    subject, body = split_subject(text)

    raw = eml_bytes(subject=subject, body=body, to="abuse@hosting.example")
    message = message_from_bytes(raw, policy=SMTP)

    assert message["To"] == "abuse@hosting.example"
    assert message["Subject"] == subject
    # The customer is the sender: their own mail program fills these in when they send it.
    assert message["From"] is None
    assert message["Date"] is None
    assert message["Message-ID"] is None
    assert message["X-Unsent"] == "1"
    # A .eml is CRLF-terminated; the text itself is otherwise carried through unchanged, so the
    # subject header plus this body is exactly the approved text again.
    assert message.get_content().replace("\r\n", "\n").rstrip("\n") == body.rstrip("\n")
    assert f"Subject: {subject}\n\n{body}" == text


def test_eml_without_a_recipient_address_has_no_to_header() -> None:
    """A web-form channel has no address; the file is still a valid message to edit."""
    message = message_from_bytes(eml_bytes(subject="s", body="b", to=""), policy=SMTP)

    assert message["To"] is None


def test_eml_headers_cannot_be_injected() -> None:
    raw = eml_bytes(
        subject="Notice\r\nBcc: someone@elsewhere.example",
        body="body",
        to="abuse@hosting.example\r\nBcc: someone@elsewhere.example",
    )
    message = message_from_bytes(raw, policy=SMTP)

    assert message["Bcc"] is None
    # Not as a header anywhere in the file either: the smuggled line is flattened into the value
    # it was hidden in, where a parser reads it as part of an address or of the subject.
    assert not any(line.startswith(b"Bcc:") for line in raw.splitlines())
    assert "\r" not in str(message["Subject"]) and "\n" not in str(message["Subject"])


def test_eml_file_names_and_types_itself() -> None:
    file = eml_file(NOTICE_ID, subject="s", body="b", to="abuse@hosting.example")

    assert file.filename == f"{NOTICE_ID}.eml"
    assert file.content_type == "message/rfc822"


@pytest.mark.parametrize("language", ["en", "de"])
def test_annex_carries_the_record_and_no_fingerprint_insight(language: str) -> None:
    text = annex_text(annex_context(language=language))

    assert "https://hoster.example/watch/123" in text
    assert "2026-09-01" in text
    assert "2026-09-17 10:00 UTC" in text
    assert NOTICE_ID in text
    assert "a" * 64 in text  # the approved text's checksum, not the text
    for forbidden in ("PDQ", "Hamming", "confidence", "band", "distance", "Konfidenz"):
        assert forbidden not in text


@pytest.mark.parametrize("language", ["en", "de"])
def test_annex_states_the_exception_review_only_where_one_was_needed(language: str) -> None:
    marker = "Schrankenprüfung" if language == "de" else "Exception review"
    silent = annex_text(annex_context(language=language))
    assert marker not in silent

    reviewed = annex_text(
        annex_context(
            language=language,
            rows=(
                AnnexRow(
                    url="https://hoster.example/watch/456",
                    first_seen=None,
                    last_checked=None,
                    extent="excerpt",
                    exception_reviewed=True,
                ),
            ),
        )
    )
    assert marker in reviewed
    assert ("ja" if language == "de" else "yes") in reviewed
    assert "—" in reviewed  # a missing date is a dash, never an invented one


def test_annex_falls_back_for_an_extent_it_does_not_know() -> None:
    text = annex_text(
        annex_context(
            rows=(
                AnnexRow(
                    url="https://hoster.example/watch/789",
                    first_seen=None,
                    last_checked=None,
                    extent="something_new",
                    exception_reviewed=None,
                ),
            )
        )
    )

    assert "not determined from the record" in text


def test_annex_file_names_itself_apart_from_the_notice() -> None:
    file = annex_file(NOTICE_ID, annex_context())

    assert file.filename == f"{NOTICE_ID}-annex.txt"
    assert file.content_type == "text/plain; charset=utf-8"


def test_a_filename_never_escapes_its_own_name() -> None:
    file = text_file("../../etc/passwd", "x")

    assert file.filename == ".._.._etc_passwd.txt"


def test_the_annex_of_a_photo_collection_says_what_was_compared() -> None:
    text = annex_text(annex_context(work_kind="photos"))

    assert "photographs" in text
    assert "audiovisual" not in text.lower()


def test_the_german_annex_of_a_photo_collection_decides_no_classification() -> None:
    # `Lichtbild` (§ 72 UrhG) and `Lichtbildwerk` (§ 2 Abs. 1 Nr. 5 UrhG) are different rights with
    # different terms: the annex says neither.
    text = annex_text(annex_context(language="de", work_kind="photos"))

    assert "Fotografien" in text
    assert "Lichtbild" not in text
    assert "audiovisuell" not in text.lower()


def test_the_video_annex_scopes_the_reviewers_finding_to_what_was_examined() -> None:
    # One finding, two documents: this is `renderers._evidence_paragraph`'s sentence, wrapped.
    text = annex_text(annex_context())

    assert (
        "   Basis: perceptual comparison with the reference work; a human reviewer reviewed\n"
        "     that comparison and recorded that the material available at the reported\n"
        "     location matches the reference work on the images examined." in text
    )


def test_the_annex_never_claims_a_whole_file_from_a_sampled_comparison() -> None:
    # `full_copy` comes from a matched in-order timeline over sampled moments
    # (`matching/copy_extent.py`), so the annex says on what the comparison rests.
    for language, expected in (
        ("en", "the reference work in full, on the moments compared"),
        ("de", "das gesamte Referenzwerk, bezogen auf die verglichenen Zeitpunkte"),
    ):
        text = annex_text(
            annex_context(
                language=language,
                rows=(
                    AnnexRow(
                        url="https://hoster.example/watch/1",
                        first_seen=date(2026, 9, 1),
                        last_checked=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
                        extent="full_copy",
                        exception_reviewed=None,
                    ),
                ),
            )
        )

        assert expected in text


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_annex_prints_each_rows_own_review_date(language: str) -> None:
    """Per row, because the locations of one draft can have been reviewed on different days
    (issue #142) — the paragraph names one date or a range; the annex is where each row's own is."""
    text = annex_text(
        annex_context(
            language=language,
            rows=(
                AnnexRow(
                    url="https://hoster.example/a",
                    first_seen=None,
                    last_checked=None,
                    extent="full_copy",
                    exception_reviewed=None,
                    reviewed_on=date(2026, 9, 2),
                ),
                AnnexRow(
                    url="https://hoster.example/b",
                    first_seen=None,
                    last_checked=None,
                    extent="full_copy",
                    exception_reviewed=None,
                    reviewed_on=date(2026, 9, 10),
                ),
            ),
        )
    )

    assert "2026-09-02" in text and "2026-09-10" in text
    assert ("Von einer Person geprüft am:" if language == "de" else "Reviewed by a person on:") in (
        text
    )


@pytest.mark.parametrize("language", ["en", "de"])
def test_a_row_with_no_recorded_review_date_omits_the_line(language: str) -> None:
    """A dash would read as "nobody reviewed it", which the gate makes impossible; the line is left
    out instead (a draft from before the date was recorded)."""
    text = annex_text(annex_context(language=language))

    assert "Von einer Person geprüft" not in text
    assert "Reviewed by a person" not in text
