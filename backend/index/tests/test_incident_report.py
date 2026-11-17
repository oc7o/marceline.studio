"""The report draft wording: both languages complete, pure rendering (issue #66)."""

from datetime import UTC, datetime

import pytest

from index import incident_report
from index.incident_report import DraftFacts, DraftUrl, Language, MediaFetch

SEEN = datetime(2026, 9, 16, 8, 30, tzinfo=UTC)


def facts(**overrides: object) -> DraftFacts:
    values: dict[str, object] = {
        "incident_id": 7,
        "status": "open",
        "categories": ("minor", "non_consent"),
        "detected_at": SEEN,
        "last_seen_at": SEEN,
        "reported_at": None,
        "hotline_reference": "",
        "media_fetch": MediaFetch.NONE,
        "urls": (
            DraftUrl(
                url="https://example.test/a",
                site="example.test",
                evidence="origin",
                first_seen_at=SEEN,
                last_seen_at=SEEN,
                primary=True,
                new_since_report=False,
            ),
        ),
        "reply_to": "reports@example.test",
    }
    values.update(overrides)
    return DraftFacts(**values)  # type: ignore[arg-type]


def test_every_phrase_exists_in_both_languages() -> None:
    assert set(incident_report.TEXT["de"]) == set(incident_report.TEXT["en"])
    tables: list[dict[str, dict[str, str]]] = [
        incident_report.CATEGORY_TEXT,
        incident_report.EVIDENCE_TEXT,
        {str(key): value for key, value in incident_report.MEDIA_FETCH_TEXT.items()},
    ]
    for table in tables:
        for key, phrases in table.items():
            assert set(phrases) == set(incident_report.LANGUAGES), key
    assert set(incident_report.MEDIA_FETCH_TEXT) == set(MediaFetch)


@pytest.mark.parametrize("language", incident_report.LANGUAGES)
def test_render_is_deterministic_and_names_every_category(language: Language) -> None:
    first = incident_report.render(facts(), language)

    assert first == incident_report.render(facts(), language)
    assert "MARCELINE-SI-7" in first.subject
    assert "minor" in first.body
    assert "non_consent" in first.body
    assert "reports@example.test" in first.body
    assert "{" not in first.body


def test_a_case_without_a_category_says_it_was_flagged_manually() -> None:
    draft = incident_report.render(facts(categories=()), "en")

    assert "flagged manually after a review of the page text (category: manual)" in draft.body


def test_unsupported_language_is_refused() -> None:
    with pytest.raises(ValueError):
        incident_report.render(facts(), "fr")  # type: ignore[arg-type]
