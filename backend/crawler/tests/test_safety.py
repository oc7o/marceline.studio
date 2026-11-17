import pytest

from catalog.models import SafetyTerm
from crawler import safety
from crawler.safety import BLOCKED, CLEAR, REVIEW, Term
from crawler.services import load_safety_terms

MINOR = SafetyTerm.Category.MINOR
NON_CONSENT = SafetyTerm.Category.NON_CONSENT
BLOCK = SafetyTerm.Action.AUTO_BLOCK_AND_INCIDENT
REVIEW_ACTION = SafetyTerm.Action.REVIEW

TERMS = safety.compile_terms(
    [
        Term("child porn", MINOR, BLOCK),
        Term(r"(?:[1-9]|1[0-7]) ?(?:y ?o|yrs? old|years? old)", MINOR, BLOCK, is_regex=True),
        Term("rape", NON_CONSENT, REVIEW_ACTION),
        Term("hidden cam", NON_CONSENT, REVIEW_ACTION),
    ]
)


def status(*texts: str) -> str:
    return safety.check(texts, TERMS).status


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Child Porn", BLOCKED),
        ("CHILD-PORN", BLOCKED),
        ("child_porn", BLOCKED),
        ("child   porn", BLOCKED),
        ("childporn", CLEAR),  # not the configured term; the seeded list has its own entry
        ("RAPE", REVIEW),
        ("grape harvest", CLEAR),
        ("rapeseed oil", CLEAR),
        ("Hidden.Cam", REVIEW),
        ("hidden camera", CLEAR),  # separate term in the seeded list
    ],
)
def test_plain_terms_match_whole_words_case_insensitively(text: str, expected: str) -> None:
    assert status(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("13yo", BLOCKED),
        ("16 y.o.", BLOCKED),
        ("17-year-old", BLOCKED),
        ("9 yrs old", BLOCKED),
        ("18yo", CLEAR),
        ("19 years old", CLEAR),
        ("21 yo", CLEAR),
        ("2013 yo", CLEAR),
        ("13 you", CLEAR),
        ("top 10 videos", CLEAR),
    ],
)
def test_age_regex_blocks_only_ages_below_18(text: str, expected: str) -> None:
    assert status(text) == expected


def test_block_wins_over_review_and_matches_are_recorded() -> None:
    verdict = safety.check(["hidden cam", "tag: 15yo"], TERMS)

    assert verdict.status == BLOCKED
    assert [term.term for term in verdict.matches] == [TERMS[1].term.term, "hidden cam"]
    assert verdict.categories == ["minor", "non_consent"]


def test_invalid_regex_is_skipped() -> None:
    compiled = safety.compile_terms([Term("(unclosed", MINOR, BLOCK, is_regex=True)])
    assert compiled == []


def test_collect_texts_walks_nested_json() -> None:
    extra = {"keywords": ["a", {"b": "c"}], "n": 3, "links": ("d",)}
    assert safety.collect_texts(extra) == ["a", "c", "d"]


@pytest.mark.parametrize(
    "text",
    [
        "Teen vlogger (18) first video",
        "18 year old teen",
        "Teen creator, just turned 18",
        "College teen 19yo",
        "Teens react",
    ],
)
def test_seeded_terms_do_not_flag_teen_alone(db: None, text: str) -> None:
    assert safety.check([text], load_safety_terms()).status == CLEAR


@pytest.mark.parametrize(
    ("text", "expected", "category"),
    [
        ("underage", BLOCKED, "minor"),
        ("Jailbait", BLOCKED, "minor"),
        ("14yo", BLOCKED, "minor"),
        ("Flagged term costume", BLOCKED, "minor"),  # customer_release_queue (2026-09-17)
        ("passed out after party", REVIEW, "non_consent"),
        ("Hidden camera", REVIEW, "non_consent"),
    ],
)
def test_seeded_terms(db: None, text: str, expected: str, category: str) -> None:
    verdict = safety.check([text], load_safety_terms())
    assert (verdict.status, verdict.categories) == (expected, [category])


def test_disabled_terms_are_not_loaded(db: None) -> None:
    SafetyTerm.objects.filter(term="jailbait").update(enabled=False)
    assert safety.check(["jailbait"], load_safety_terms()).status == CLEAR


def test_invalid_regex_fails_model_validation(db: None) -> None:
    from django.core.exceptions import ValidationError

    term = SafetyTerm(term="(x", is_regex=True, category=MINOR, action=BLOCK)
    with pytest.raises(ValidationError):
        term.full_clean()


REVIEW_MATCH = {"term": "rape", "category": "non_consent", "action": "review"}


def test_resolve_new_page_takes_the_verdict() -> None:
    verdict = safety.check(["rape"], TERMS)
    resolution = safety.resolve(None, [], False, verdict)
    assert (resolution.status, resolution.matches) == (REVIEW, [REVIEW_MATCH])


def test_resolve_keeps_a_human_decision_for_known_terms() -> None:
    verdict = safety.check(["rape"], TERMS)
    resolution = safety.resolve(CLEAR, [REVIEW_MATCH], True, verdict)
    assert (resolution.status, resolution.keep_review) == (CLEAR, True)


def test_resolve_flags_reviewed_pages_again_for_new_terms() -> None:
    verdict = safety.check(["rape", "hidden cam"], TERMS)
    resolution = safety.resolve(CLEAR, [REVIEW_MATCH], True, verdict)
    assert (resolution.status, resolution.keep_review) == (REVIEW, False)


def test_resolve_never_unblocks_automatically() -> None:
    match = {"term": "child porn", "category": "minor", "action": "block"}
    resolution = safety.resolve(BLOCKED, [match], False, safety.check(["fine"], TERMS))
    assert (resolution.status, resolution.matches) == (BLOCKED, [match])


def test_resolve_clears_review_pages_whose_terms_no_longer_match() -> None:
    resolution = safety.resolve(REVIEW, [REVIEW_MATCH], False, safety.check(["fine"], TERMS))
    assert (resolution.status, resolution.matches) == (CLEAR, [])
