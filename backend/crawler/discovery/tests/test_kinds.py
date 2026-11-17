"""Coverage verdicts and the structure counters behind them (issue #118).

Synthetic HTML written for these tests only — no recorded site markup, no request, no media
(root `CLAUDE.md`). The kinds and the verdicts are the ones
`docs/research/creator-leak-sources.md` decides between.
"""

import pytest
from django.db import IntegrityError, transaction

from crawler.discovery.kinds import Coverage, ProbeFacts, blocks_onboarding, classify_probe
from crawler.discovery.services import record_coverage
from crawler.discovery.structure import (
    forum_paths,
    gallery_image_count,
    profile_paths,
)
from crawler.models import SourceCandidate

BASE = "https://example.test/"


def _page(body: str) -> str:
    return f"<html><head><title>t</title></head><body>{body}</body></html>"


# --------------------------------------------------------------------------- structure counters


def test_profile_links_are_counted_and_the_index_itself_is_not() -> None:
    body = "".join(f'<a href="/model/person-{n}">p</a>' for n in range(4))
    body += '<a href="/models">index</a><a href="/models/2">page 2</a>'
    body += '<a href="https://elsewhere.test/model/other">off site</a>'
    assert len(profile_paths(_page(body), BASE)) == 4


def test_forum_links_are_counted_from_path_and_from_query() -> None:
    body = '<a href="/threads/abc.123/">t</a><a href="/forum/board-2/">b</a>'
    body += '<a href="/viewtopic.php?t=91">t</a><a href="/about">no</a>'
    assert len(forum_paths(_page(body), BASE)) == 3


def test_the_biggest_same_shaped_picture_family_is_what_counts() -> None:
    body = "".join(f'<img src="/i/set-9/{n}.jpg">' for n in range(8))
    body += '<img src="/static/logo.png"><img src="/static/avatar.png">'
    assert gallery_image_count(_page(body), BASE) == 8


def test_a_lazy_gallery_is_seen_through_its_data_attribute() -> None:
    body = "".join(f'<img data-src="/i/set-9/{n}.jpg" src="/static/blank.gif">' for n in range(7))
    assert gallery_image_count(_page(body), BASE) == 7


# ------------------------------------------------------------------------------------- verdicts


@pytest.mark.parametrize(
    ("facts", "kind", "coverage"),
    [
        (ProbeFacts(domain_kind="file_hoster"), "file_hoster", Coverage.LINK_INTELLIGENCE),
        (ProbeFacts(domain_kind="torrent"), "torrent", Coverage.LINK_INTELLIGENCE),
        (ProbeFacts(domain_kind="social"), "social", Coverage.PARKED),
        (ProbeFacts(), "unknown", Coverage.UNKNOWN),
        (
            ProbeFacts(probed=True, robots_allows=True, blocked_reason="login"),
            "unknown",
            Coverage.NOT_COVERABLE,
        ),
        (
            ProbeFacts(probed=True, blocked_reason="challenge or CAPTCHA page (HTTP 403)"),
            "unknown",
            Coverage.NOT_COVERABLE,
        ),
        (
            ProbeFacts(probed=True, robots_allows=False),
            "unknown",
            Coverage.NOT_COVERABLE,
        ),
        (
            ProbeFacts(probed=True, blocked_reason="age_verification_provider: ID/selfie check"),
            "unknown",
            Coverage.NOT_COVERABLE,
        ),
        (
            ProbeFacts(probed=True, robots_allows=True, video_links_seen=9),
            "tube",
            Coverage.COVERABLE,
        ),
        (
            ProbeFacts(probed=True, robots_allows=True, profile_links_seen=12),
            "name_index",
            Coverage.COVERABLE,
        ),
        (
            ProbeFacts(probed=True, robots_allows=True, gallery_images_seen=20),
            "gallery",
            Coverage.COVERABLE,
        ),
        (
            ProbeFacts(probed=True, robots_allows=True, forum_links_seen=30),
            "forum",
            Coverage.NOT_COVERABLE,
        ),
        (
            ProbeFacts(probed=True, robots_allows=None),
            "unknown",
            Coverage.UNKNOWN,
        ),
    ],
)
def test_the_verdict_follows_our_own_rules(
    facts: ProbeFacts, kind: str, coverage: Coverage
) -> None:
    verdict = classify_probe(facts)
    assert (verdict.kind, verdict.coverage) == (kind, coverage)
    assert verdict.reason


def test_a_self_declaration_age_gate_stays_coverable_and_names_its_approval() -> None:
    verdict = classify_probe(
        ProbeFacts(
            probed=True,
            robots_allows=True,
            blocked_reason="age_self_declaration",
            video_links_seen=3,
        )
    )
    assert (verdict.kind, verdict.coverage) == ("tube", Coverage.COVERABLE)
    assert "age_confirmation" in verdict.reason


def test_only_the_two_blocking_verdicts_block_onboarding() -> None:
    blocking = {value for value in Coverage if blocks_onboarding(value)}
    assert blocking == {Coverage.NOT_COVERABLE, Coverage.PARKED}


# ------------------------------------------------------------------------- the onboarding guard


@pytest.mark.django_db
def test_a_probed_candidate_gets_its_verdict_recorded() -> None:
    candidate = SourceCandidate.objects.create(domain="example.test")
    candidate.probed_at = candidate.first_seen_at
    candidate.robots_allows = True
    candidate.profile_links_seen = 11
    record_coverage(candidate)
    assert (candidate.kind, candidate.coverage) == ("name_index", Coverage.COVERABLE)


@pytest.mark.django_db
def test_an_approved_candidate_keeps_the_operator_s_decision() -> None:
    candidate = SourceCandidate.objects.create(
        domain="example.test", status=SourceCandidate.Status.APPROVED, coverage=Coverage.COVERABLE
    )
    candidate.probed_at = candidate.first_seen_at
    candidate.robots_allows = False
    record_coverage(candidate)
    assert candidate.coverage == Coverage.COVERABLE


@pytest.mark.django_db
def test_the_database_refuses_to_approve_what_our_rules_forbid() -> None:
    candidate = SourceCandidate.objects.create(
        domain="example.test", coverage=Coverage.NOT_COVERABLE
    )
    candidate.status = SourceCandidate.Status.APPROVED
    with transaction.atomic(), pytest.raises(IntegrityError):
        candidate.save(update_fields=("status",))
