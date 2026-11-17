"""Safety rule actions of 2026-09-17: word boundaries, auto-block without review, rule versions."""

import pytest
from django.utils import timezone

from catalog import safety_rules
from catalog.models import SafetyRuleSet, SafetyTerm, Site
from crawler import safety, services
from crawler.models import CrawlTask
from crawler.safety import BLOCKED, CLEAR, REVIEW, Term
from crawler.services import load_safety_terms
from crawler.sites.base import PageData, ParseResult
from index.models import Page, SafetyRuleDecision
from index.services import page_media_allowed

AUTO_BLOCK = safety_rules.AUTO_BLOCK
INCIDENT = safety_rules.AUTO_BLOCK_AND_INCIDENT
QUEUE = safety_rules.CUSTOMER_RELEASE_QUEUE


def seeded(text: str) -> safety.Verdict:
    return safety.check([text], load_safety_terms())


@pytest.mark.parametrize(
    "text",
    ["rape", "Raped girl", "RAPING scene", "she rapes", "Forced", "forced-entry roleplay"],
)
def test_non_consent_words_auto_block(db: None, text: str) -> None:
    verdict = seeded(text)
    assert verdict.status == BLOCKED
    assert verdict.categories == ["non_consent"]
    assert {term.action for term in verdict.matches} == {AUTO_BLOCK}
    assert not verdict.incident_due


@pytest.mark.parametrize(
    "text",
    [
        "grape harvest",
        "draped in silk",
        "drape",
        "enforced rules",
        "reinforced frame",
        "forcedly smiling",
        "rapeseed field",
        "scraped knee",
        "Force of habit",
    ],
)
def test_look_alikes_do_not_match(db: None, text: str) -> None:
    assert seeded(text).status == CLEAR


@pytest.mark.parametrize("text", ["lolita", "Lolita", "LOLITA dress", "LoLiTa", "two lolitas"])
def test_lolita_blocks_as_csam_suspect_with_incident(db: None, text: str) -> None:
    verdict = seeded(text)
    assert verdict.status == BLOCKED
    assert verdict.categories == ["minor"]
    assert {term.action for term in verdict.matches} == {INCIDENT}
    assert verdict.incident_due


@pytest.mark.parametrize("text", ["lolitaland resort", "xlolita", "lolitas2"])
def test_lolita_whole_word_only(db: None, text: str) -> None:
    assert seeded(text).status == CLEAR


@pytest.mark.parametrize(
    "text", ["flagged term", "Flagged Term outfit", "FLAGGED TERM"]
)
def test_flagged_term_blocks_into_the_release_rule(db: None, text: str) -> None:
    verdict = seeded(text)
    assert verdict.status == BLOCKED
    assert {term.action for term in verdict.matches} == {QUEUE}
    assert verdict.release_queue_only
    assert not verdict.incident_due


def test_flagged_term_with_another_hit_is_not_release_only(db: None) -> None:
    verdict = seeded("flagged term hidden cam")
    assert verdict.status == BLOCKED
    assert not verdict.release_queue_only


def test_former_block_terms_open_incidents(db: None) -> None:
    assert set(SafetyTerm.objects.values_list("action", flat=True)) <= set(safety_rules.ACTIONS)
    assert SafetyTerm.objects.get(term="jailbait").action == INCIDENT
    assert seeded("Hidden camera").status == REVIEW


def test_unknown_or_legacy_actions_block_and_count_as_incidents() -> None:
    assert safety_rules.canonical_action("block") == INCIDENT
    assert safety_rules.blocks("block") and safety_rules.blocks("something new")
    assert not safety_rules.blocks("review")
    legacy = [{"term": "x", "category": "minor", "action": "block"}]
    assert safety_rules.incident_due(legacy) and safety_rules.opens_incident(legacy)
    verdict = safety.check(["x"], safety.compile_terms([Term("x", "minor", "surprise")]))
    assert verdict.status == BLOCKED


@pytest.mark.parametrize(
    ("actions", "due"),
    [
        ([], True),  # a human blocked it by hand: a case, as always
        (["review"], True),  # blocked by a human despite review terms only
        (["auto_block"], False),
        (["customer_release_queue"], False),
        (["auto_block", "customer_release_queue"], False),
        (["auto_block", "auto_block_and_incident"], True),
        (["customer_release_queue", "review"], False),
    ],
)
def test_incident_due(actions: list[str], due: bool) -> None:
    matches = [{"term": f"t{i}", "category": "minor", "action": a} for i, a in enumerate(actions)]
    assert safety_rules.incident_due(matches) is due


def test_rule_version_is_content_addressed() -> None:
    one = [("rape", False, "non_consent", AUTO_BLOCK), ("lolita", False, "minor", INCIDENT)]
    assert safety_rules.rule_version(one) == safety_rules.rule_version(list(reversed(one)))
    changed = [("rape", False, "non_consent", "review"), ("lolita", False, "minor", INCIDENT)]
    assert safety_rules.rule_version(one) != safety_rules.rule_version(changed)
    legacy = [("rape", False, "non_consent", AUTO_BLOCK), ("lolita", False, "minor", "block")]
    assert safety_rules.rule_version(one) == safety_rules.rule_version(legacy)


def test_a_review_decision_does_not_cover_a_stricter_rule() -> None:
    terms = safety.compile_terms([Term("rape", "non_consent", AUTO_BLOCK)])
    cleared_as_review = [{"term": "rape", "category": "non_consent", "action": "review"}]
    resolution = safety.resolve(CLEAR, cleared_as_review, True, safety.check(["rape"], terms))
    assert (resolution.status, resolution.keep_review) == (BLOCKED, False)

    staff_unblocked = [{"term": "rape", "category": "non_consent", "action": AUTO_BLOCK}]
    resolution = safety.resolve(CLEAR, staff_unblocked, True, safety.check(["rape"], terms))
    assert (resolution.status, resolution.keep_review) == (CLEAR, True)


def test_legacy_block_decisions_cover_the_renamed_action() -> None:
    terms = safety.compile_terms([Term("jailbait", "minor", INCIDENT)])
    legacy = [{"term": "jailbait", "category": "minor", "action": "block"}]
    resolution = safety.resolve(CLEAR, legacy, True, safety.check(["jailbait"], terms))
    assert resolution.keep_review


@pytest.fixture
def site01(db: None) -> Site:
    return Site.objects.create(domain="site01.example", crawl_delay_s=0)


def store(site: Site, title: str, slug: str = "1") -> Page:
    url = f"https://site01.example/video/{slug}/x/"
    task, _ = CrawlTask.objects.get_or_create(
        url=url, defaults={"site": site, "kind": CrawlTask.Kind.DETAIL}
    )
    page = services.store_parse_result(
        task, ParseResult(page=PageData(url=url, title=title, tags=("outdoor",)))
    )
    assert page is not None
    page.refresh_from_db()
    return page


def test_auto_blocked_page_skips_the_review_queue_and_is_logged(site01: Site) -> None:
    page = store(site01, "Forced roleplay in the office")

    assert page.safety_status == Page.SafetyStatus.BLOCKED
    assert not Page.objects.filter(safety_status=Page.SafetyStatus.REVIEW).exists()
    assert not page_media_allowed(page.safety_status, page.safety_score)
    [decision] = SafetyRuleDecision.objects.filter(page=page)
    assert decision.kind == SafetyRuleDecision.Kind.AUTO_BLOCK
    assert decision.terms == [{"term": "forced", "category": "non_consent", "action": AUTO_BLOCK}]
    version = safety.rule_version(load_safety_terms())
    assert decision.rule_version == page.safety_rule_version == version
    snapshot = SafetyRuleSet.objects.get(version=version)
    assert {
        "term": "forced",
        "is_regex": False,
        "category": "non_consent",
        "action": AUTO_BLOCK,
    } in (snapshot.terms)

    again = store(site01, "Forced roleplay in the office")
    assert again.pk == page.pk
    assert SafetyRuleDecision.objects.filter(page=page).count() == 1  # same matches: no new row


def test_a_page_reviewed_under_the_old_rule_is_blocked_again(site01: Site) -> None:
    page = store(site01, "Nice clean title for everyone")
    Page.objects.filter(pk=page.pk).update(
        title="Rape fantasy roleplay title",
        safety_status=Page.SafetyStatus.CLEAR,
        safety_matches=[{"term": "rape", "category": "non_consent", "action": "review"}],
        safety_reviewed_at=page.first_seen,
    )

    page = store(site01, "Rape fantasy roleplay title")

    assert page.safety_status == Page.SafetyStatus.BLOCKED
    assert page.safety_reviewed_at is None


def test_site_risk_counts_only_blocks_that_are_hotline_cases(site01: Site) -> None:
    rows: list[list[dict[str, str]]] = [
        [{"term": "forced", "category": "non_consent", "action": AUTO_BLOCK}],
        [{"term": "flagged term", "category": "minor", "action": QUEUE}],
        [{"term": "jailbait", "category": "minor", "action": INCIDENT}],
        [{"term": "jailbait", "category": "minor", "action": "block"}],
        [
            {"term": "forced", "category": "non_consent", "action": AUTO_BLOCK},
            {"term": "lolita", "category": "minor", "action": INCIDENT},
        ],
        [],  # blocked by hand
    ]
    for index, matches in enumerate(rows):
        Page.objects.create(
            site=site01,
            url=f"https://site01.example/risk/{index}",
            safety_status=Page.SafetyStatus.BLOCKED,
            safety_flagged_at=timezone.now(),
            safety_matches=matches,
        )

    assert services.site_risk(site01).recent_blocks == 4
