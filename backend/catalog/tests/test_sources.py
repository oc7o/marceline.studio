from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from catalog.models import Creator, Site, SiteHost
from catalog.services import seed_sources
from catalog.sources import (
    SITE_POLICIES,
    AgeConfirmation,
    AgeConfirmationError,
    parse_age_confirmation,
    parse_sources,
)

APPROVAL: dict[str, Any] = {
    "cookies": {"age_verified": "1"},
    "approved_by": "operator",
    "approved_at": "2026-09-15",
    "note": "click-through",
}

SOURCES = """Sites:
- https://site01.example/
- https://www.site15.example/
- https://unknown.test/
- site01.example

Creators:
- Lena Hartwell
- Dana Forde
- Lena Hartwell

Anything outside the two lists is ignored.
"""


def test_parse_sources_normalizes_and_dedupes() -> None:
    sources = parse_sources(SOURCES)

    assert sources.sites == ("site01.example", "site15.example", "unknown.test")
    assert sources.creators == ("Lena Hartwell", "Dana Forde")


def test_site04_policy_is_enabled_with_polite_delay() -> None:
    policy = SITE_POLICIES["site04.example"]

    assert policy.enabled
    assert policy.crawl_delay_s >= 3.0
    assert "mirror of site02.example" in policy.notes


def test_kvs_template_sites_are_enabled_with_polite_delay() -> None:
    for domain in ("site19.example", "site18.example", "site22.example"):
        policy = SITE_POLICIES[domain]

        assert policy.enabled
        assert policy.crawl_delay_s >= 3.0
        assert policy.notes == (
            "Enabled 2026-09-15: KVS template adapter; discovered via source discovery"
        )


def test_discovered_own_adapter_sites_are_enabled_with_polite_delay() -> None:
    for domain in ("site05.example", "site06.example"):
        policy = SITE_POLICIES[domain]

        assert policy.enabled
        assert policy.crawl_delay_s >= 3.0
        assert policy.notes == (
            "Enabled 2026-09-15: operator approved; discovered via source discovery"
        )
    assert "unknown-site.example" not in SITE_POLICIES


def test_site25_stays_disabled_behind_age_verification() -> None:
    policy = SITE_POLICIES["site25.example"]

    assert not policy.enabled
    assert "age-verification" in policy.notes


def test_seed_sources_applies_the_policy_delay_and_budget(db: None) -> None:
    seed_sources(parse_sources("Sites:\n- https://www.site18.example/\n- https://www.site25.example/\n"))

    site18 = Site.objects.get(domain="site18.example")
    assert site18.enabled
    assert site18.crawl_delay_s == 3.0
    assert (site18.daily_request_budget, site18.daily_media_request_budget) == (3000, 3000)
    assert not Site.objects.get(domain="site25.example").enabled


def test_seed_sources_marks_reviewed_cdn_subdomains_but_keeps_operator_rows(db: None) -> None:
    seed_sources(parse_sources("Sites:\n- site05.example\n- site09.example\n"))
    site05 = Site.objects.get(domain="site05.example")
    row = SiteHost.objects.get(site=site05, host="static-eu-cdn.site05.example")
    assert (row.role, row.source) == ("cdn", "policy")

    site09 = Site.objects.get(domain="site09.example")
    SiteHost.objects.filter(site=site09).update(role="origin", source="operator")
    seed_sources(parse_sources("Sites:\n- site05.example\n- site09.example\n"))
    assert SiteHost.objects.get(site=site09).role == "origin"
    assert SiteHost.objects.filter(site=site05).count() == 1


def test_a_declared_crawl_delay_is_never_lowered(db: None) -> None:
    """site03.example declares Crawl-delay 10 in robots.txt; our own delay never undercuts it."""
    assert SITE_POLICIES["site03.example"].crawl_delay_s == 10.0

    seed_sources(parse_sources("Sites:\n- https://site03.example/\n"))

    assert Site.objects.get(domain="site03.example").crawl_delay_s == 10.0


def test_seed_sources_never_touches_the_capture_switch(db: None) -> None:
    """`Site.capture_enabled` (issue #78) is an operator decision, not part of `SitePolicy`, so a
    reseed must never flip it back on behind the operator's back."""
    seed_sources(parse_sources("Sites:\n- site01.example\n"))
    site = Site.objects.get(domain="site01.example")
    assert site.capture_enabled is True  # default: no behaviour change until the operator acts

    Site.objects.filter(pk=site.pk).update(capture_enabled=False)
    seed_sources(parse_sources("Sites:\n- site01.example\n"))

    assert Site.objects.get(domain="site01.example").capture_enabled is False


def test_seed_sources_never_touches_the_play_click_switch(db: None) -> None:
    """`Site.capture_play_click` (ADR 0016 amendment 2026-09-18) is the same kind of decision:
    off for every site until the operator approves one, and never re-applied by a reseed."""
    seed_sources(parse_sources("Sites:\n- site01.example\n"))
    site = Site.objects.get(domain="site01.example")
    assert site.capture_play_click is False  # no site clicks anything by default

    Site.objects.filter(pk=site.pk).update(capture_play_click=True)
    seed_sources(parse_sources("Sites:\n- site01.example\n"))

    assert Site.objects.get(domain="site01.example").capture_play_click is True


def test_seed_sources_is_idempotent_and_applies_policies(db: None) -> None:
    sources = parse_sources(SOURCES)

    first = seed_sources(sources)
    second = seed_sources(sources)

    assert (first.sites_created, first.creators_created) == (3, 2)
    assert (second.sites_created, second.creators_created) == (0, 0)
    assert Site.objects.get(domain="site01.example").enabled
    site15 = Site.objects.get(domain="site15.example")
    assert not site15.enabled
    assert "Cloudflare" in site15.notes
    assert not Site.objects.get(domain="unknown.test").enabled
    assert Creator.objects.count() == 2


def test_seed_sources_keeps_creator_aliases_and_unreviewed_site_settings(db: None) -> None:
    Creator.objects.create(name="Lena Hartwell", aliases=["lenahartwell"])
    Site.objects.create(domain="unknown.test", enabled=True)

    seed_sources(parse_sources(SOURCES))

    assert Creator.objects.get(name="Lena Hartwell").aliases == ["lenahartwell"]
    assert Site.objects.get(domain="unknown.test").enabled


def test_seed_sources_command_reads_file(db: None, tmp_path: Path) -> None:
    path = tmp_path / "sources.md"
    path.write_text(SOURCES)
    out = StringIO()

    call_command("seed_sources", path=str(path), stdout=out)

    assert Site.objects.count() == 3
    assert "2 creator(s) (2 new)" in out.getvalue()


def test_age_confirmation_parses_operator_approval() -> None:
    assert parse_age_confirmation({}) is None
    assert parse_age_confirmation(APPROVAL) == AgeConfirmation(
        {"age_verified": "1"}, "operator", "2026-09-15", "click-through"
    )


@pytest.mark.parametrize(
    "value",
    [
        {**APPROVAL, "approved_by": ""},
        {**APPROVAL, "approved_at": "yesterday"},
        {**APPROVAL, "cookies": {}},
        {**APPROVAL, "cookies": {"age_verified": "1; Path=/"}},
        {**APPROVAL, "cookies": {"PHPSESSID": "abc"}},
        {**APPROVAL, "cookies": {"cf_clearance": "abc"}},
        {**APPROVAL, "query": {"age": "1"}},
        ["age_verified"],
    ],
)
def test_age_confirmation_rejects_malformed_or_non_age_cookies(value: Any) -> None:
    with pytest.raises(AgeConfirmationError):
        parse_age_confirmation(value)


def test_site_validation_reports_invalid_age_confirmation(db: None) -> None:
    site = Site(domain="gate.test", age_confirmation={"cookies": {"age_verified": "1"}})

    with pytest.raises(ValidationError) as exc:
        site.full_clean()

    assert "age_confirmation" in exc.value.message_dict
    assert Site(domain="open.test").age_confirmation == {}
