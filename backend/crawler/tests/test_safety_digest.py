"""The one daily operator report: docs/safety.md, crawler/safety_digest.py."""

from datetime import UTC, datetime, timedelta

import pytest
from django.core import mail
from django.core.mail import EmailMessage
from django.core.management import call_command
from django.utils import timezone
from pytest_django.fixtures import Settings

from catalog.models import Site, SiteRobotsPolicy
from crawler import safety_digest
from crawler.models import AdapterHealth
from index.models import Page, SafetyIncident, SafetyRuleDecision

NOW = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)
IDENTIFYING_TITLE = "a very identifying scene title"
PAGE_URL = "https://example.test/watch/identifying-slug"

INCIDENT_RULE = {"term": "jailbait", "category": "minor", "action": "auto_block_and_incident"}
RELEASE_RULE = {"term": "flagged term", "category": "minor", "action": "customer_release_queue"}
NON_CONSENT_RULE = {"term": "forced", "category": "non_consent", "action": "auto_block"}


@pytest.fixture
def site(db: None) -> Site:
    return Site.objects.create(domain="site01.example", crawl_delay_s=0)


@pytest.fixture
def other_site(db: None) -> Site:
    return Site.objects.create(domain="site06.example", crawl_delay_s=0)


def configure_mail(settings: Settings) -> None:
    settings.SAFETY_ALERT_TO = "ops@example.test"
    settings.SAFETY_ALERT_FROM = "marceline@example.test"


def blocked(
    site: Site,
    slug: str,
    *,
    rule: dict[str, str] = INCIDENT_RULE,
    at: datetime = NOW - timedelta(hours=1),
    title: str = "",
    url: str = "",
) -> Page:
    return Page.objects.create(
        site=site,
        url=url or f"https://{site.domain}/{slug}",
        title=title or f"title for {slug}",
        safety_status=Page.SafetyStatus.BLOCKED,
        safety_matches=[rule],
        safety_flagged_at=at,
    )


def block(domain: str, *rules: dict[str, str], page_id: int = 1) -> safety_digest.Block:
    return safety_digest.Block(
        page_id=page_id,
        site_domain=domain,
        rules=safety_digest.parse_rules(list(rules)),
    )


# --- pure counting -----------------------------------------------------------------------------


def test_tally_counts_pages_per_site_and_per_rule() -> None:
    result = safety_digest.tally(
        [
            block("a.test", INCIDENT_RULE),
            block("a.test", RELEASE_RULE),
            block("b.test", RELEASE_RULE, NON_CONSENT_RULE),
        ]
    )

    assert result.pages == 3
    assert result.by_site == (("a.test", 2), ("b.test", 1))
    assert dict((rule.term, count) for rule, count in result.by_rule) == {
        "jailbait": 1,
        "flagged term": 2,
        "forced": 1,
    }


def test_the_incident_class_is_exactly_what_was_mailed_at_once() -> None:
    """The digest's "already mailed" count and `alerts.mails_immediately` must not disagree."""
    result = safety_digest.tally(
        [
            block("a.test", INCIDENT_RULE),
            block("a.test", RELEASE_RULE),
            block("a.test", NON_CONSENT_RULE),
            block("a.test", RELEASE_RULE, INCIDENT_RULE),
            block("a.test"),  # a human's block: no rule matched, nothing was mailed
        ]
    )

    assert (result.pages, result.incident_pages) == (5, 2)


def test_a_legacy_block_action_still_counts_as_the_incident_class() -> None:
    """`block` predates the four actions (catalog/safety_rules.py); unknown means strictest."""
    legacy = safety_digest.tally([block("a.test", {**INCIDENT_RULE, "action": "block"})])
    unknown = safety_digest.tally([block("a.test", {**INCIDENT_RULE, "action": "typo"})])

    assert legacy.incident_pages == 1
    assert unknown.incident_pages == 1


def test_malformed_matches_never_break_the_digest() -> None:
    assert safety_digest.parse_rules("not a list") == ()
    assert safety_digest.parse_rules([None, 7, {"term": "x"}]) == (
        safety_digest.Rule(action="", category="", term="x"),
    )


# --- the mail ------------------------------------------------------------------------------------


def digest_of(
    *,
    incident_blocks: tuple[safety_digest.Block, ...] = (),
    adapter_alarms: tuple[safety_digest.AdapterAlarm, ...] = (),
    tdm_reservations: tuple[safety_digest.TdmReservation, ...] = (),
    data_requests: safety_digest.DataRequests | None = None,
    **counts: int,
) -> safety_digest.Digest:
    blocks = [block(f"site{n}.test", INCIDENT_RULE) for n in range(counts.pop("sites", 1))]
    return safety_digest.Digest(
        start=NOW - timedelta(hours=24),
        end=NOW,
        current=safety_digest.tally(blocks),
        previous=safety_digest.tally([]),
        incidents_open=counts.pop("incidents_open", 0),
        incidents_reported=counts.pop("incidents_reported", 0),
        incidents_opened=counts.pop("incidents_opened", 0),
        release_queue_waiting=counts.pop("release_queue_waiting", 0),
        released=counts.pop("released", 0),
        queued=counts.pop("queued", 0),
        staff_changes=counts.pop("staff_changes", 0),
        incident_blocks=incident_blocks,
        adapter_alarms=adapter_alarms,
        tdm_reservations=tdm_reservations,
        data_requests=data_requests,
    )


def test_long_lists_are_summarised_rather_than_hidden() -> None:
    digest = digest_of(sites=safety_digest.SITE_LINES + 3)

    body = safety_digest.body(digest)

    assert f"{safety_digest.SITE_LINES + 3} page(s) newly blocked" in body
    assert "and 3 more site(s): 3" in body


def test_a_quiet_window_says_nothing_is_waiting() -> None:
    body = safety_digest.body(digest_of())

    assert "Needs a look: nothing is waiting." in body
    assert "safety incident(s) open" not in body


def test_what_needs_a_human_is_named_with_its_admin_link() -> None:
    digest = digest_of(incidents_open=6, incidents_reported=2, release_queue_waiting=5)

    body = safety_digest.body(digest)

    assert "6 safety incident(s) open, 2 reported and not settled" in body
    assert "/admin/index/safetyincident/" in body
    assert "5 page(s) waiting in the customer release queue" in body
    assert "safety_release__exact=queued" in body


def test_the_subject_carries_counts_only() -> None:
    subject = safety_digest.subject(digest_of(sites=2, incidents_open=1))

    assert (
        subject
        == "Marceline daily report 2026-09-18: 2 blocked (+2), 2 by an incident rule, 1 waiting"
    )


def test_the_subject_adds_crawler_counts_only_when_nonzero() -> None:
    quiet = safety_digest.subject(digest_of())
    assert "adapter alarm" not in quiet
    assert "TDM reservation" not in quiet

    loud = safety_digest.subject(
        digest_of(
            adapter_alarms=(safety_digest.AdapterAlarm(domain="a.test", detail="blind 90%"),),
            tdm_reservations=(
                safety_digest.TdmReservation(
                    site_id=1,
                    host="a.test",
                    names_us=False,
                    names_wildcard=False,
                    third_party_ai=True,
                    paused=False,
                ),
            ),
        )
    )
    assert "1 adapter alarm(s)" in loud
    assert "1 TDM reservation(s)" in loud


def test_the_incident_blocks_are_listed_by_name() -> None:
    digest = digest_of(
        incident_blocks=(
            safety_digest.Block(
                page_id=42,
                site_domain="a.test",
                rules=safety_digest.parse_rules([INCIDENT_RULE]),
            ),
        )
    )

    body = safety_digest.body(digest)

    assert "- page 42 - site a.test - category minor" in body
    assert 'term "jailbait"' in body
    assert "/admin/index/page/42/change/" in body


def test_an_empty_incident_block_list_says_none() -> None:
    body = safety_digest.body(digest_of())

    assert "Blocked by an incident rule in this window: none." in body


def test_the_crawler_section_lists_adapter_alarms_and_tdm_reservations() -> None:
    digest = digest_of(
        adapter_alarms=(safety_digest.AdapterAlarm(domain="a.test", detail="blind 90% of 40"),),
        tdm_reservations=(
            safety_digest.TdmReservation(
                site_id=7,
                host="a.test",
                names_us=True,
                names_wildcard=False,
                third_party_ai=False,
                paused=True,
            ),
        ),
    )

    body = safety_digest.body(digest)

    assert "== Crawler ==" in body
    assert "1 adapter(s) newly reported broken" in body
    assert "- a.test: blind 90% of 40" in body
    assert "/admin/crawler/adapterhealth/" in body
    assert "1 site(s) with a new or changed robots.txt reservation" in body
    assert "- site 7 host a.test: names us - PAUSED pending review" in body
    assert "/admin/catalog/siterobotspolicy/" in body


def test_an_empty_crawler_section_says_nothing_to_report() -> None:
    body = safety_digest.body(digest_of())

    assert "Adapter health: nothing to report." in body
    assert "TDM/AI reservations: nothing new in this window." in body


def test_the_footer_says_this_mail_always_arrives() -> None:
    body = safety_digest.body(digest_of())

    assert "sent once a day even when every section above is quiet" in body
    assert "If this mail stops arriving, the reporting pipeline itself is broken." in body


# --- reading the database ------------------------------------------------------------------------


def test_collect_counts_only_the_window_and_compares_with_the_one_before(
    site: Site, other_site: Site
) -> None:
    blocked(site, "in-window", at=NOW - timedelta(hours=2))
    blocked(other_site, "also-in-window", rule=RELEASE_RULE, at=NOW - timedelta(hours=3))
    blocked(site, "yesterday", at=NOW - timedelta(hours=30))
    blocked(site, "much-older", at=NOW - timedelta(days=5))
    # Flagged inside the window but not blocked: the review queue is not the digest's business.
    Page.objects.create(
        site=site,
        url="https://site01.example/review",
        title="review",
        safety_status=Page.SafetyStatus.REVIEW,
        safety_flagged_at=NOW - timedelta(hours=2),
    )

    digest = safety_digest.collect(NOW - timedelta(hours=24), NOW)

    assert digest.current.pages == 2
    assert digest.current.incident_pages == 1
    assert digest.previous.pages == 1
    assert digest.delta == 1
    assert digest.current.by_site == (("site01.example", 1), ("site06.example", 1))


def test_collect_reports_the_backlog_and_the_window_s_release_steps(site: Site) -> None:
    page = blocked(site, "one", rule=RELEASE_RULE)
    page.safety_release = Page.SafetyRelease.QUEUED
    page.save(update_fields=("safety_release",))
    SafetyIncident.objects.create(
        status=SafetyIncident.Status.OPEN, detected_at=NOW, last_seen_at=NOW
    )
    SafetyIncident.objects.create(
        status=SafetyIncident.Status.REPORTED,
        detected_at=NOW,
        last_seen_at=NOW,
        reported_at=NOW,
    )
    for kind in (
        SafetyRuleDecision.Kind.RELEASED,
        SafetyRuleDecision.Kind.QUEUED,
        SafetyRuleDecision.Kind.STAFF_STATUS,
    ):
        SafetyRuleDecision.objects.create(page=page, kind=kind)

    digest = safety_digest.collect(NOW - timedelta(hours=24), timezone.now() + timedelta(hours=1))

    assert (digest.incidents_open, digest.incidents_reported) == (1, 1)
    assert digest.release_queue_waiting == 1
    assert (digest.released, digest.queued, digest.staff_changes) == (1, 1, 1)


def test_load_adapter_alarms_reads_the_window(site: Site) -> None:
    AdapterHealth.objects.create(
        site=site,
        status=AdapterHealth.Status.BROKEN,
        detail="blind 92% of 40",
        notified_at=NOW - timedelta(hours=2),
    )
    other = Site.objects.create(domain="outside-window.test")
    AdapterHealth.objects.create(
        site=other,
        status=AdapterHealth.Status.BROKEN,
        detail="blind 95% of 40",
        notified_at=NOW - timedelta(hours=30),  # before the window
    )

    alarms = safety_digest.load_adapter_alarms(NOW - timedelta(hours=24), NOW)

    assert [a.domain for a in alarms] == ["site01.example"]
    assert alarms[0].detail == "blind 92% of 40"


def test_load_adapter_alarms_ignores_a_row_never_notified(site: Site) -> None:
    AdapterHealth.objects.create(site=site, status=AdapterHealth.Status.BROKEN, notified_at=None)

    assert safety_digest.load_adapter_alarms(NOW - timedelta(hours=24), NOW) == []


def test_load_tdm_reservations_reads_the_window_and_paused_state(
    site: Site, settings: Settings
) -> None:
    settings.ROBOTS_RESERVATION_PAUSE_ENABLED = True
    site.paused_until = NOW + timedelta(days=1)
    site.pause_reason = "tdm_reservation: reserved against us"
    site.save(update_fields=("paused_until", "pause_reason"))
    SiteRobotsPolicy.objects.create(
        site=site,
        host=site.domain,
        content_hash="abc",
        names_us=True,
        changed_at=NOW,
        checked_at=NOW,
        notified_at=NOW - timedelta(hours=1),
        notified_hash="abc",
    )
    other = Site.objects.create(domain="outside-window.test")
    SiteRobotsPolicy.objects.create(
        site=other,
        host=other.domain,
        content_hash="def",
        third_party_ai=True,
        changed_at=NOW,
        checked_at=NOW,
        notified_at=NOW - timedelta(hours=30),  # before the window
        notified_hash="def",
    )

    rows = safety_digest.load_tdm_reservations(NOW - timedelta(hours=24), NOW, now=NOW)

    assert [(r.site_id, r.host, r.paused) for r in rows] == [(site.pk, site.domain, True)]


def test_the_digest_never_carries_a_page_url_or_title(site: Site, settings: Settings) -> None:
    configure_mail(settings)
    blocked(site, "one", title=IDENTIFYING_TITLE, url=PAGE_URL)

    safety_digest.send_digest(NOW - timedelta(hours=24), NOW)

    [sent] = mail.outbox
    text = f"{sent.subject}\n{sent.body}"
    assert IDENTIFYING_TITLE not in text
    assert PAGE_URL not in text
    assert "site01.example" in text  # the site domain is allowed, and is the point of the mail
    assert "jailbait" in text  # our own rule term, like the immediate alert (docs/safety.md)


def test_one_mail_for_the_whole_window(site: Site, other_site: Site, settings: Settings) -> None:
    configure_mail(settings)
    for index in range(5):
        blocked(site, f"a{index}")
    for index in range(3):
        blocked(other_site, f"b{index}", rule=RELEASE_RULE)

    summary = safety_digest.send_digest(NOW - timedelta(hours=24), NOW)

    [sent] = mail.outbox
    assert sent.to == ["ops@example.test"]
    assert sent.from_email == "marceline@example.test"
    assert "8 page(s) newly blocked" in sent.body
    assert "5 of them by a rule that opens a safety incident" in sent.body
    assert "mailed" in summary


def test_a_fully_quiet_window_still_sends_one_short_mail(site: Site, settings: Settings) -> None:
    """2026-09-19: the absence of this mail, not its content, is now the "something broke" signal.

    A block outside the window (so the window itself has nothing) still gets exactly one mail,
    with every section saying so and the footer explaining why it still arrived."""
    configure_mail(settings)
    blocked(site, "yesterday", at=NOW - timedelta(hours=30))

    summary = safety_digest.send_digest(NOW - timedelta(hours=24), NOW)

    [sent] = mail.outbox
    assert "0 page(s) newly blocked" in sent.body
    assert "Adapter health: nothing to report." in sent.body
    assert "TDM/AI reservations: nothing new in this window." in sent.body
    assert "reporting pipeline itself is broken" in sent.body
    assert "mailed" in summary


def test_nothing_is_sent_while_the_addresses_are_unset(site: Site, settings: Settings) -> None:
    settings.SAFETY_ALERT_TO = ""
    settings.SAFETY_ALERT_FROM = ""
    blocked(site, "one")

    summary = safety_digest.send_digest(NOW - timedelta(hours=24), NOW)

    assert mail.outbox == []
    assert "SAFETY_ALERT_TO" in summary


def test_a_failed_send_raises_so_the_scheduler_never_advances_the_window(
    site: Site, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`marceline/scheduler.py` `run_job` only stamps `last_succeeded_at` on success — a send that
    raises here means the next run's window starts from the same point, not the next day."""
    configure_mail(settings)
    blocked(site, "one")

    def explode(*args: object, **kwargs: object) -> int:
        raise OSError("no mail server")

    monkeypatch.setattr(EmailMessage, "send", explode)

    with pytest.raises(OSError):
        safety_digest.send_digest(NOW - timedelta(hours=24), NOW)


def test_the_window_runs_from_the_last_successful_run() -> None:
    last = NOW - timedelta(hours=26)

    assert safety_digest.window(NOW, last) == (last, NOW)
    assert safety_digest.window(NOW, None) == (NOW - safety_digest.DEFAULT_WINDOW, NOW)
    # A clock that went backwards must never produce a window ending before it starts.
    assert safety_digest.window(NOW, NOW + timedelta(hours=1)) == (NOW, NOW)


# --- the command ---------------------------------------------------------------------------------


def test_the_command_dry_run_prints_the_mail_and_sends_nothing(
    site: Site, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_mail(settings)
    blocked(site, "one", at=timezone.now() - timedelta(minutes=5))

    call_command("safety_digest", "--dry-run")

    out = capsys.readouterr().out
    assert "Marceline daily report" in out
    assert "not sent (dry run)" in out
    assert mail.outbox == []


def test_the_command_mails_the_last_day(
    site: Site, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_mail(settings)
    blocked(site, "one", at=timezone.now() - timedelta(minutes=5))

    call_command("safety_digest", "--hours", "24")

    assert len(mail.outbox) == 1
    assert "mailed" in capsys.readouterr().out


# --- the creator's own data requests (ADR 0056, issue #133) --------------------------------------


def test_the_mail_says_how_many_data_requests_are_waiting() -> None:
    digest = digest_of(data_requests=safety_digest.DataRequests(waiting=2, oldest_days=9))

    body = safety_digest.body(digest)

    assert "== Customers ==" in body
    assert "Data requests waiting: 2 (oldest: 9 days)" in body
    # One month per request, and the mail says so rather than deciding for the operator.
    assert "Art. 12(3) GDPR" in body
    assert "/admin/marceline/customerdatarequest/" in body
    # It is the one statutory deadline in this mail, so it reaches the subject line too.
    assert "2 data request(s) open" in safety_digest.subject(digest)


def test_nothing_waiting_is_a_line_and_not_a_missing_section() -> None:
    body = safety_digest.body(
        digest_of(data_requests=safety_digest.DataRequests(waiting=0, oldest_days=None))
    )

    assert "Data requests waiting: 0." in body
    assert "data request(s) open" not in safety_digest.subject(
        digest_of(data_requests=safety_digest.DataRequests(waiting=0, oldest_days=None))
    )


def test_a_machine_that_could_not_count_says_so_rather_than_claiming_zero() -> None:
    body = safety_digest.body(digest_of(data_requests=None))

    assert "Data requests waiting: not counted on this machine." in body


@pytest.mark.django_db
def test_the_count_comes_from_the_hook_the_project_package_installs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`crawler` may not import `marceline`, so `marceline/apps.py` installs the counter.

    Installed for real at startup; here it is set by hand, which is also what proves the digest
    reads it through the hook and holds no query of its own.
    """
    assert safety_digest.load_data_requests() is not None, "installed by marceline.apps.ready"

    monkeypatch.setattr(safety_digest, "data_request_source", None)
    assert safety_digest.load_data_requests() is None

    monkeypatch.setattr(
        safety_digest,
        "data_request_source",
        lambda: safety_digest.DataRequests(waiting=4, oldest_days=1),
    )
    assert safety_digest.load_data_requests() == safety_digest.DataRequests(
        waiting=4, oldest_days=1
    )


def test_a_counter_that_raises_never_loses_the_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode() -> safety_digest.DataRequests:
        raise RuntimeError("no database")

    monkeypatch.setattr(safety_digest, "data_request_source", explode)

    assert safety_digest.load_data_requests() is None
