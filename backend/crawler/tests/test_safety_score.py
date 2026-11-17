"""The per-page safety score (ADR 0018). Pure: no database, no clock."""

from django.conf import settings

from crawler import safety, safety_score
from crawler.safety_score import DEFAULT_MIN_SCORE, PageScore, SiteRisk, score_page

# Deliberate Cyrillic test data for the language signal; the mixed-script string is intentional.
RUSSIAN_TEXT = "Русское домашнее видео"  # noqa: RUF001
MIXED_SCRIPT = "abcабв"  # noqa: RUF001

TERMS = safety.compile_terms(
    [
        safety.Term("underage", "minor", "block"),
        safety.Term("hidden cam", "non_consent", "review"),
    ]
)


def score(*texts: str, risk: SiteRisk = safety_score.NO_SITE_RISK) -> PageScore:
    return score_page(texts, safety.check(texts, TERMS), site_risk=risk)


def test_the_default_threshold_matches_the_setting() -> None:
    # Two names for one number; they must not drift apart.
    assert settings.SAFETY_GATE_MIN_SCORE == DEFAULT_MIN_SCORE


def test_an_ordinary_page_scores_full_marks() -> None:
    result = score("Lena Hartwell scene", "outdoor", "interview")

    assert result.score == 1.0
    assert result.reason == safety_score.CLEARLY_SAFE
    assert result.signals == ()
    assert result.is_clearly_safe()


def test_block_and_review_terms_score_zero() -> None:
    blocked = score("underage something")
    review = score("hidden cam scene")

    assert (blocked.score, blocked.reason) == (0.0, safety_score.BLOCK_TERM)
    assert (review.score, review.reason) == (0.0, safety_score.REVIEW_TERM)
    assert not blocked.is_clearly_safe() and not review.is_clearly_safe()


def test_an_age_in_an_uncovered_unit_is_a_near_miss_and_gates_the_page() -> None:
    # "15 jahre" is not a seeded term (those cover yo / yrs old / years old) but must hold media.
    result = score("Sie ist 15 jahre alt")

    assert result.reason == safety_score.AGE_PATTERN
    assert not result.is_clearly_safe()
    assert result.score < DEFAULT_MIN_SCORE


def test_minor_context_clusters_accumulate_up_to_a_cap() -> None:
    one = score("a staged flagged term fantasy scene")
    three = score("a staged flagged term stepdaughter babyface scene")

    assert one.reason == safety_score.MINOR_CONTEXT
    assert one.score == 1.0 - safety_score.PENALTIES[safety_score.MINOR_CONTEXT]
    # A single mild signal is still clearly safe; several are not.
    assert one.is_clearly_safe()
    assert three.score == 1.0 - safety_score.MAX_MINOR_CONTEXT_PENALTY
    assert not three.is_clearly_safe()


def test_a_bare_low_number_only_nudges_the_score() -> None:
    result = score("scene 14 of the series")

    assert result.reason == safety_score.BARE_LOW_AGE
    assert result.is_clearly_safe()  # far too noisy to gate on its own


def test_text_in_a_barely_covered_language_loses_points() -> None:
    result = score(RUSSIAN_TEXT)

    assert safety_score.UNCOVERED_LANGUAGE in result.reason
    assert result.score == 1.0 - safety_score.PENALTIES[safety_score.UNCOVERED_LANGUAGE]


def test_almost_no_text_is_a_signal_of_its_own() -> None:
    result = score("clip")

    assert result.reason == safety_score.THIN_TEXT


def test_site_risk_rises_with_recent_blocks_and_is_capped() -> None:
    one = score("an entirely ordinary scene title", risk=SiteRisk("x.test", 1))
    many = score("an entirely ordinary scene title", risk=SiteRisk("x.test", 99))

    assert one.score == 1.0 - safety_score.SITE_RISK_PER_BLOCK
    assert many.score == 1.0 - safety_score.MAX_SITE_RISK_PENALTY
    assert many.reason == safety_score.SITE_RISK


def test_site_risk_combines_with_a_text_signal_to_gate_a_page() -> None:
    # Neither alone gates at the default threshold; together they do. This is the site06 case:
    # Russian text on a site with two recent block incidents.
    text_only = score(RUSSIAN_TEXT)
    both = score(RUSSIAN_TEXT, risk=SiteRisk("site06.example", 2))

    assert text_only.is_clearly_safe()
    assert not both.is_clearly_safe()


def test_the_score_never_leaves_its_range() -> None:
    worst = score("15 jahre flagged term stepdaughter babyface Русское", risk=SiteRisk("x", 99))

    assert worst.score == safety_score.MIN_SCORE


def test_signals_list_every_reason_that_fired() -> None:
    result = score("a staged flagged term scene, part 14", risk=SiteRisk("x.test", 1))

    assert result.signals == (
        f"{safety_score.BARE_LOW_AGE}",
        f"{safety_score.MINOR_CONTEXT}:school",
        f"{safety_score.SITE_RISK}:1",
    )


def test_cyrillic_share_ignores_digits_and_punctuation() -> None:
    assert safety_score.cyrillic_share("") == 0.0
    assert safety_score.cyrillic_share("123 ...") == 0.0
    assert safety_score.cyrillic_share("абв") == 1.0
    assert safety_score.cyrillic_share(MIXED_SCRIPT) == 0.5
