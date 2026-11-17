"""Recording a robots.txt reservation, and what follows from it (tripwire T2, issue #105)."""

import pytest
from django.core import mail
from django.utils import timezone
from pytest_django.fixtures import Settings

from catalog import robots_policy
from catalog.models import Site, SiteRobotsPolicy
from catalog.request_report import site_lines
from core.robots import describe_robots

UA = "MarcelineBot/0.1"

NAMED_AI_ONLY = """User-agent: *
Disallow: /search/

User-agent: GPTBot
Disallow: /

User-agent: CCBot
Disallow: /
"""
NAMES_US = NAMED_AI_ONLY + "\nUser-agent: MarcelineBot\nDisallow: /\n"
NO_RESERVATION = "User-agent: *\nDisallow: /search/\n"


def _record(site: Site, body: str, headers: dict[str, str] | None = None) -> robots_policy.Outcome:
    outcome = robots_policy.record(site.pk, site.domain, describe_robots(body, UA, headers or {}))
    assert outcome is not None
    return outcome


@pytest.mark.django_db
def test_a_third_party_reservation_is_recorded_and_changes_nothing_else(site: Site) -> None:
    outcome = _record(site, NAMED_AI_ONLY)

    assert outcome.third_party_reservation and not outcome.reserved_against_us
    assert not outcome.paused
    site.refresh_from_db()
    assert site.paused_until is None
    row = SiteRobotsPolicy.objects.get(site=site)
    assert row.ai_tokens == ["ccbot", "gptbot"]
    assert row.third_party_ai and not row.names_us and not row.names_wildcard


@pytest.mark.django_db
def test_re_reading_the_same_file_changes_nothing_and_alerts_once(
    site: Site, settings: Settings
) -> None:
    settings.ADAPTER_ALERT_TO = "operator@example.test"
    settings.ADAPTER_ALERT_FROM = "marceline@example.test"
    # 2026-09-19: a TDM/AI reservation folds into the daily report by default; this test exercises
    # the still-supported immediate path.
    settings.OPERATOR_MAIL_IMMEDIATE = "tdm_reservation"

    first = _record(site, NAMED_AI_ONLY)
    row = SiteRobotsPolicy.objects.get(site=site)
    changed_at = row.changed_at
    second = _record(site, NAMED_AI_ONLY)

    assert first.changed and not second.changed
    row.refresh_from_db()
    assert row.changed_at == changed_at
    assert row.checked_at >= changed_at
    assert len(mail.outbox) == 1
    assert str(site.pk) in mail.outbox[0].subject
    assert "gptbot" in mail.outbox[0].body


@pytest.mark.django_db
def test_an_extended_reservation_alerts_again(site: Site, settings: Settings) -> None:
    settings.ADAPTER_ALERT_TO = "operator@example.test"
    settings.ADAPTER_ALERT_FROM = "marceline@example.test"
    settings.OPERATOR_MAIL_IMMEDIATE = "tdm_reservation"

    _record(site, NAMED_AI_ONLY)
    outcome = _record(site, NAMES_US)

    assert outcome.changed and outcome.reserved_against_us
    assert len(mail.outbox) == 2


@pytest.mark.django_db
def test_no_immediate_mail_by_default_even_when_addressed(site: Site, settings: Settings) -> None:
    """2026-09-19: a new/changed reservation no longer mails on its own by default — it still
    stamps `notified_at`/`notified_hash` (which the daily report reads) and still logs once."""
    settings.ADAPTER_ALERT_TO = "operator@example.test"
    settings.ADAPTER_ALERT_FROM = "marceline@example.test"

    outcome = _record(site, NAMED_AI_ONLY)

    assert outcome.third_party_reservation
    assert mail.outbox == []
    row = SiteRobotsPolicy.objects.get(site=site)
    assert row.notified_at is not None
    assert row.notified_hash == row.content_hash


@pytest.mark.django_db
def test_a_reservation_naming_us_pauses_the_site_only_when_the_switch_is_on(
    site: Site, settings: Settings
) -> None:
    settings.ROBOTS_RESERVATION_PAUSE_ENABLED = False
    off = _record(site, NAMES_US)
    site.refresh_from_db()
    assert not off.paused and site.paused_until is None

    SiteRobotsPolicy.objects.filter(site=site).delete()
    settings.ROBOTS_RESERVATION_PAUSE_ENABLED = True
    on = _record(site, NAMES_US)
    site.refresh_from_db()

    assert on.paused
    assert site.paused_until is not None and site.paused_until > timezone.now()
    assert site.pause_reason.startswith(robots_policy.TDM_PAUSE_REASON)
    assert robots_policy.paused_for_reservation(site)


@pytest.mark.django_db
def test_a_reservation_header_counts_even_without_a_group(site: Site, settings: Settings) -> None:
    settings.ROBOTS_RESERVATION_PAUSE_ENABLED = True

    outcome = _record(site, NO_RESERVATION, {"x-robots-tag": "noai"})

    site.refresh_from_db()
    assert outcome.reserved_against_us and outcome.paused
    assert SiteRobotsPolicy.objects.get(site=site).reservation_headers == ["x-robots-tag"]


@pytest.mark.django_db
def test_detection_off_records_nothing(site: Site, settings: Settings) -> None:
    settings.ROBOTS_RESERVATION_DETECTION_ENABLED = False

    assert robots_policy.record(site.pk, site.domain, describe_robots(NAMES_US, UA)) is None
    assert not SiteRobotsPolicy.objects.exists()


@pytest.mark.django_db
def test_the_report_names_the_tokens_and_never_a_path(site: Site) -> None:
    _record(site, NAMED_AI_ONLY)

    lines = site_lines(site, [], [], days=7, now=timezone.now())
    reservation = [line for line in lines if "robots reservation" in line]

    assert reservation and "gptbot" in reservation[0]
    assert "/search/" not in "\n".join(lines)


@pytest.mark.django_db
def test_the_counts_are_per_site_and_only_for_enabled_sites(site: Site) -> None:
    _record(site, NAMED_AI_ONLY)
    other = Site.objects.create(domain="disabled.test", enabled=False)
    robots_policy.record(other.pk, other.domain, describe_robots(NAMES_US, UA))

    counts = robots_policy.counts(timezone.now())

    assert counts.third_party == 1
    assert counts.naming_us == 0
    assert counts.paused == 0
