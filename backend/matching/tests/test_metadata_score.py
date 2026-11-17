"""The pure metadata rule (ADR 0040): what admits a pair, and what may never happen.

No database here. The rule is two independent coincidences, and the tests are written against the
measurement in docs/matching/metadata-scoring.md rather than against the implementation.
"""

from matching.metadata_score import (
    CAST_DURATION_TOLERANCE_S,
    TITLE_MIN_JACCARD,
    MetadataInputs,
    MetadataReason,
    score_metadata,
    title_tokens,
    token_jaccard,
)

CAST = frozenset({7, 9})
SHARES_ONE = frozenset({9, 11})
SHARES_NONE = frozenset({11, 13})


def pair(
    *,
    page_duration: float | None = 600.0,
    original_duration: float | None = 600.0,
    page_cast: frozenset[int] = SHARES_ONE,
    page_title: str = "",
    original_title: str = "",
) -> MetadataInputs:
    return MetadataInputs(
        page_duration_s=page_duration,
        original_duration_s=original_duration,
        page_creator_ids=page_cast,
        original_creator_ids=CAST,
        page_title_tokens=title_tokens(page_title),
        original_title_tokens=title_tokens(original_title),
    )


def test_a_shared_performer_and_a_matching_duration_admit_the_pair() -> None:
    signal = score_metadata(pair(page_duration=601.0))
    assert signal.reason == MetadataReason.CAST_AND_DURATION
    assert signal.proposes
    assert signal.signals["shared_cast"] == 1
    assert signal.signals["duration_difference_s"] == 1.0


def test_the_cast_clause_stops_at_its_documented_tolerance() -> None:
    inside = score_metadata(pair(page_duration=600.0 + CAST_DURATION_TOLERANCE_S))
    outside = score_metadata(pair(page_duration=600.0 + CAST_DURATION_TOLERANCE_S + 0.1))
    assert inside.reason == MetadataReason.CAST_AND_DURATION
    # Just outside it, the wide tolerance alone is not enough: it needs the title too.
    assert outside.reason == MetadataReason.NO_SIGNAL


def test_a_shared_performer_alone_admits_nothing() -> None:
    """The creator link is a filter, not a discriminator: 66 % of rejected pairs have one."""
    assert not score_metadata(pair(page_duration=120.0)).proposes


def test_a_matching_duration_alone_admits_nothing() -> None:
    signal = score_metadata(pair(page_duration=601.0, page_cast=SHARES_NONE))
    assert signal.reason == MetadataReason.NO_SIGNAL
    assert signal.signals["shared_cast"] == 0


def test_the_title_clause_needs_no_creator_link_at_all() -> None:
    """54.8 % of active pages credit nobody; this clause is the only thing that reaches them."""
    signal = score_metadata(
        pair(
            page_duration=615.0,  # inside the wide tolerance, outside the cast clause
            page_cast=SHARES_NONE,
            page_title="the long winter drive home",
            original_title="the long winter drive",
        )
    )
    assert signal.reason == MetadataReason.DURATION_AND_TITLE
    assert float(signal.signals["title_jaccard"]) >= TITLE_MIN_JACCARD


def test_the_title_clause_still_needs_the_duration() -> None:
    signal = score_metadata(
        pair(
            page_duration=60.0,
            page_cast=SHARES_NONE,
            page_title="the long winter drive home",
            original_title="the long winter drive",
        )
    )
    assert signal.reason == MetadataReason.NO_SIGNAL


def test_a_thin_title_overlap_admits_nothing() -> None:
    signal = score_metadata(
        pair(page_duration=615.0, page_cast=SHARES_NONE, page_title="winter", original_title="the")
    )
    assert signal.reason == MetadataReason.NO_SIGNAL


def test_a_missing_duration_is_no_information_and_never_a_match() -> None:
    assert score_metadata(pair(page_duration=None)).reason == MetadataReason.NO_PAGE_DURATION
    assert (
        score_metadata(pair(original_duration=None)).reason == MetadataReason.NO_ORIGINAL_DURATION
    )
    assert not score_metadata(pair(page_duration=None)).proposes
    assert not score_metadata(pair(original_duration=None)).proposes


def test_a_photo_collection_has_no_duration_so_the_signal_is_silent() -> None:
    """A collection's `duration_s` is NULL by constraint; PDQ reaches it, this does not."""
    signal = score_metadata(pair(original_duration=None, page_duration=600.0))
    assert not signal.proposes


def test_a_clip_is_never_penalised_only_unproposed() -> None:
    """Clips are leaks: a shorter page loses nothing here, it simply earns nothing.

    "The page is clearly shorter" was measured on the labelled pool at 18.4 % of confirmed leaks
    against 42.7 % of rejected pairs — it is not a signal in either direction, so there is no
    reason code for it and no penalty anywhere.
    """
    clip = score_metadata(pair(page_duration=45.0, original_duration=1800.0))
    assert clip.reason == MetadataReason.NO_SIGNAL
    assert not clip.proposes
    assert "penalty" not in clip.signals
    # And a clip that PDQ matched is unaffected: this module writes nothing for it either way.
    assert set(clip.signals) == {
        "scorer_version",
        "shared_cast",
        "title_jaccard",
        "duration_difference_s",
        "duration_tolerance_s",
    }


def test_the_signal_carries_no_score_to_threshold_on() -> None:
    """Deliberate: a number here would invite comparison with a real match's confidence."""
    signal = score_metadata(pair(page_duration=601.0))
    assert not hasattr(signal, "score")
    assert not hasattr(signal, "confidence")


def test_title_tokens_drop_punctuation_case_and_short_noise() -> None:
    assert title_tokens("The Long  Winter-Drive, HD 4k!") == {"the", "long", "winter", "drive"}
    assert title_tokens("") == frozenset()


def test_token_jaccard_is_symmetric_and_bounded() -> None:
    left, right = title_tokens("one two three"), title_tokens("two three four")
    assert token_jaccard(left, right) == token_jaccard(right, left)
    assert token_jaccard(left, left) == 1.0
    assert token_jaccard(left, frozenset()) == 0.0
    assert token_jaccard(frozenset(), frozenset()) == 0.0


def test_no_minimum_token_count_because_it_only_cost_recall() -> None:
    """Measured: a three-token floor dropped 5 of 148 candidates, one of them a confirmed leak."""
    assert token_jaccard(title_tokens("winter drive"), title_tokens("winter drive")) == 1.0
