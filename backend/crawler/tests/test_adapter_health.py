"""Noticing that a site's adapter has stopped extracting (issue #22, crawler/adapter_health.py)."""

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from catalog.models import Site
from crawler import adapter_alerts, adapter_health
from crawler.models import AdapterHealth, AdapterParseStat, CrawlTask
from crawler.sites.base import MediaSpec, PageData, ParseResult, has_card_media_only
from crawler.sites.registry import ADAPTERS
from index.models import Media

#: The adapters whose video pages carry no media of their own, so every poster and frame travels
#: with the task from the listing card. Named here so a new one cannot be added quietly: the
#: declaration turns off half the breakage rule for that site.
CARD_MEDIA_ONLY = {"site04.example", "site02.example", "site07.example"}

BROKEN = AdapterHealth.Status.BROKEN
OK = AdapterHealth.Status.OK
UNKNOWN = AdapterHealth.Status.UNKNOWN


@pytest.fixture
def site01(db: None) -> Site:
    return Site.objects.create(domain="site01.example", crawl_delay_s=0, enabled=True)


@pytest.fixture
def site04(db: None) -> Site:
    """A site whose media come from the listing card (`card_media_only`)."""
    return Site.objects.create(domain="site04.example", crawl_delay_s=0, enabled=True)


def good_result(title: str = "a scene") -> ParseResult:
    return ParseResult(
        page=PageData(
            url="https://site01.example/v/1", title=title, duration_s=600, performers=("Someone",)
        ),
        media=(MediaSpec(Media.Kind.POSTER, "https://cdn.test/1.jpg"),),
    )


# --- the pure half ------------------------------------------------------------------------------


def test_a_full_parse_is_not_blind() -> None:
    outcome = adapter_health.outcome_of(good_result())
    assert not outcome.blind
    assert not outcome.no_page and not outcome.no_title and not outcome.no_media


def test_no_page_at_all_is_the_loudest_outcome() -> None:
    """The commonest break: the selector finds nothing, so `parse` returns no page."""
    outcome = adapter_health.outcome_of(ParseResult())
    assert outcome.no_page and outcome.blind
    # Nothing else is claimed about a parse that produced no page, so the hourly sums stay
    # separable (`_sum` relies on it).
    assert not outcome.no_title and not outcome.no_media


def test_an_empty_title_or_no_media_is_blind_too() -> None:
    no_title = adapter_health.outcome_of(
        ParseResult(page=PageData(url="u", title="   "), media=(MediaSpec(Media.Kind.POSTER, "x"),))
    )
    assert no_title.no_title and no_title.blind
    no_media = adapter_health.outcome_of(ParseResult(page=PageData(url="u", title="t")))
    assert no_media.no_media and no_media.blind


def test_a_missing_duration_or_cast_is_recorded_but_never_blind() -> None:
    """Measured: those two swing 0.8 % -> 16.9 % day to day on a healthy adapter. Never alarm."""
    outcome = adapter_health.outcome_of(
        ParseResult(
            page=PageData(url="u", title="t"),
            media=(MediaSpec(Media.Kind.POSTER, "x"),),
        )
    )
    assert outcome.no_duration and outcome.no_performers
    assert not outcome.blind


def test_the_threshold_is_far_above_the_measured_normal() -> None:
    """0.026 % of stored pages have no media and 0.00 % no title (38,110 pages, 2026-09-18)."""
    normal = adapter_health.Window(parses=1000, blind=1)
    assert adapter_health.judge(normal, adapter_health.Window(), was_broken=False).status == OK
    broken = adapter_health.Window(parses=1000, blind=400)
    assert adapter_health.judge(broken, adapter_health.Window(), was_broken=False).broken


def test_too_few_parses_is_unknown_not_ok() -> None:
    thin = adapter_health.Window(parses=adapter_health.MIN_PARSES - 1, blind=0)
    assert adapter_health.judge(thin, adapter_health.Window(), was_broken=False).status == UNKNOWN


def test_silence_never_repairs_an_adapter() -> None:
    """A broken parser often means no pages at all; that must not read as recovery."""
    thin = adapter_health.Window(parses=1, blind=1)
    assert adapter_health.judge(thin, adapter_health.Window(), was_broken=True).broken


def test_a_broken_adapter_needs_real_recovery_not_just_a_dip() -> None:
    """Hysteresis: below BROKEN_SHARE but above RECOVERED_SHARE keeps the site broken."""
    wobbling = adapter_health.Window(parses=200, blind=40)  # 20 %
    assert adapter_health.judge(wobbling, adapter_health.Window(), was_broken=False).status == OK
    assert adapter_health.judge(wobbling, adapter_health.Window(), was_broken=True).broken
    recovered = adapter_health.Window(parses=200, blind=4)  # 2 %
    assert adapter_health.judge(recovered, adapter_health.Window(), was_broken=True).status == OK


def test_the_verdict_says_what_it_was_decided_on() -> None:
    verdict = adapter_health.judge(
        adapter_health.Window(parses=100, blind=90),
        adapter_health.Window(parses=500, blind=2),
        was_broken=False,
    )
    assert "90 of 100" in verdict.detail and "baseline" in verdict.detail


# --- counters and verdicts ----------------------------------------------------------------------


@pytest.mark.django_db
def test_recording_a_parse_adds_to_one_hourly_bucket(site01: Site) -> None:
    now = timezone.now()
    adapter_health.record_parse(site01.pk, adapter_health.outcome_of(good_result()), now=now)
    adapter_health.record_parse(site01.pk, adapter_health.outcome_of(ParseResult()), now=now)
    row = AdapterParseStat.objects.get(site=site01)
    # A parse that produced no page claims nothing else, so `no_media` stays 0 (see `_sum`).
    assert (row.parses, row.no_page, row.no_title, row.no_media) == (2, 1, 0, 0)
    assert row.hour == adapter_health.hour_floor(now)


@pytest.mark.django_db
def test_a_repair_is_counted_without_inventing_a_parse(site01: Site) -> None:
    adapter_health.record_repair(site01.pk)
    row = AdapterParseStat.objects.get(site=site01)
    assert (row.parses, row.llm_repaired) == (0, 1)


@pytest.mark.django_db
def test_evaluate_stores_the_verdict_and_the_numbers_behind_it(site01: Site) -> None:
    now = timezone.now()
    for _ in range(60):
        adapter_health.record_parse(site01.pk, adapter_health.ParseOutcome(no_page=True), now=now)
    for _ in range(40):
        adapter_health.record_parse(
            site01.pk, adapter_health.outcome_of(good_result()), now=now - timedelta(hours=2)
        )
    report = adapter_health.evaluate(site01, now=now)
    assert report.status == BROKEN
    record = AdapterHealth.objects.get(site=site01)
    assert record.status == BROKEN
    assert record.parses == 100
    assert record.blind_share == pytest.approx(0.6)
    assert record.broken_since == now


@pytest.mark.django_db
def test_a_healthy_site_is_ok_and_keeps_no_broken_since(site01: Site) -> None:
    now = timezone.now()
    for _ in range(50):
        adapter_health.record_parse(site01.pk, adapter_health.outcome_of(good_result()), now=now)
    report = adapter_health.evaluate(site01, now=now)
    assert report.status == OK
    assert AdapterHealth.objects.get(site=site01).broken_since is None


@pytest.mark.django_db
def test_broken_since_is_the_first_sighting_not_the_latest_pass(site01: Site) -> None:
    first = timezone.now() - timedelta(hours=3)
    for _ in range(40):
        adapter_health.record_parse(site01.pk, adapter_health.ParseOutcome(no_page=True), now=first)
    adapter_health.evaluate(site01, now=first)
    later = first + timedelta(hours=2)
    for _ in range(40):
        adapter_health.record_parse(site01.pk, adapter_health.ParseOutcome(no_page=True), now=later)
    adapter_health.evaluate(site01, now=later)
    assert AdapterHealth.objects.get(site=site01).broken_since == first


@pytest.mark.django_db
def test_counters_outside_the_window_do_not_decide(site01: Site) -> None:
    now = timezone.now()
    old = now - timedelta(hours=adapter_health.WINDOW_HOURS + 1)
    for _ in range(80):
        adapter_health.record_parse(site01.pk, adapter_health.ParseOutcome(no_page=True), now=old)
    for _ in range(40):
        adapter_health.record_parse(site01.pk, adapter_health.outcome_of(good_result()), now=now)
    report = adapter_health.evaluate(site01, now=now)
    assert report.status == OK
    assert report.baseline.blind == 80


@pytest.mark.django_db
def test_a_dry_run_writes_no_verdict(site01: Site) -> None:
    now = timezone.now()
    for _ in range(40):
        adapter_health.record_parse(site01.pk, adapter_health.ParseOutcome(no_page=True), now=now)
    reports = adapter_health.evaluate_all(now=now, write=False)
    assert [report.status for report in reports] == [BROKEN]
    assert not AdapterHealth.objects.exists()


@pytest.mark.django_db
def test_the_report_names_domains_and_counters_only(site01: Site) -> None:
    now = timezone.now()
    for _ in range(40):
        adapter_health.record_parse(site01.pk, adapter_health.ParseOutcome(no_page=True), now=now)
    text = adapter_health.render_text(adapter_health.evaluate_all(now=now))
    assert "site01.example" in text and "look broken" in text
    assert "https://" not in text


# --- the operator alert ---------------------------------------------------------------------


@pytest.mark.django_db
def test_a_new_break_mails_the_operator_once(site01: Site) -> None:
    from django.test import override_settings

    now = timezone.now()
    for _ in range(40):
        adapter_health.record_parse(site01.pk, adapter_health.ParseOutcome(no_page=True), now=now)
    reports = adapter_health.evaluate_all(now=now)
    with override_settings(
        SAFETY_ALERT_TO="ops@example.test",
        SAFETY_ALERT_FROM="bot@example.test",
        # 2026-09-19: an adapter alarm folds into the daily report by default; this test exercises
        # the still-supported immediate path.
        OPERATOR_MAIL_IMMEDIATE="adapter_health",
    ):
        assert adapter_alerts.notify_broken(reports) == 1
        assert len(mail.outbox) == 1
        body = mail.outbox[0].body
        assert "site01.example" in body
        # Never a page, a URL or any extracted text: this is about a parser.
        assert "https://" not in body
        # A second pass in the same window stays quiet.
        assert adapter_alerts.notify_broken(adapter_health.evaluate_all(now=now)) == 0
        assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_nothing_is_sent_for_a_healthy_adapter(site01: Site) -> None:
    from django.test import override_settings

    now = timezone.now()
    for _ in range(40):
        adapter_health.record_parse(site01.pk, adapter_health.outcome_of(good_result()), now=now)
    with override_settings(
        SAFETY_ALERT_TO="ops@example.test",
        SAFETY_ALERT_FROM="bot@example.test",
        OPERATOR_MAIL_IMMEDIATE="adapter_health",
    ):
        assert adapter_alerts.notify_broken(adapter_health.evaluate_all(now=now)) == 0
    assert mail.outbox == []


@pytest.mark.django_db
def test_no_immediate_mail_by_default_even_when_addressed(site01: Site) -> None:
    """2026-09-19: a new break no longer mails on its own by default — it still records the
    `AdapterHealth.notified_at` stamp the daily report reads, and still logs once."""
    from django.test import override_settings

    now = timezone.now()
    for _ in range(40):
        adapter_health.record_parse(site01.pk, adapter_health.ParseOutcome(no_page=True), now=now)
    reports = adapter_health.evaluate_all(now=now)
    with override_settings(
        SAFETY_ALERT_TO="ops@example.test", SAFETY_ALERT_FROM="bot@example.test"
    ):
        assert adapter_alerts.notify_broken(reports) == 1

    assert mail.outbox == []
    assert AdapterHealth.objects.get(site=site01).notified_at is not None


@pytest.mark.django_db
def test_an_unreachable_mailbox_never_breaks_the_pass(
    site01: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    from django.core.mail import EmailMessage
    from django.test import override_settings

    now = timezone.now()
    for _ in range(40):
        adapter_health.record_parse(site01.pk, adapter_health.ParseOutcome(no_page=True), now=now)
    reports = adapter_health.evaluate_all(now=now)

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("no mail server")

    monkeypatch.setattr(EmailMessage, "send", explode)
    with override_settings(
        SAFETY_ALERT_TO="ops@example.test",
        SAFETY_ALERT_FROM="bot@example.test",
        OPERATOR_MAIL_IMMEDIATE="adapter_health",
    ):
        assert adapter_alerts.notify_broken(reports) == 0


# --- the worker writes the counters -------------------------------------------------------------


@pytest.mark.django_db
def test_the_crawl_worker_counts_what_the_adapter_yielded(site01: Site) -> None:
    """The recording is in `CrawlWorker`, not the adapter: adapters stay pure."""
    from crawler.worker import CrawlWorker

    task = CrawlTask.objects.create(
        site=site01, url="https://site01.example/v/1", kind=CrawlTask.Kind.DETAIL
    )
    worker = CrawlWorker.__new__(CrawlWorker)  # no client needed for this path
    adapter = ADAPTERS["site01.example"]
    result = worker._watch_extraction(task, ParseResult(), "<html></html>", adapter)
    assert result.page is None  # nothing repaired: the repair is off by default
    row = AdapterParseStat.objects.get(site=site01)
    assert (row.parses, row.no_page) == (1, 1)


# --- a site whose media come from the listing card (ADR 0042 amendment 2026-09-19) --------------


def test_only_the_named_adapters_say_their_detail_pages_carry_no_media() -> None:
    """The declaration switches off half the breakage rule, so it is spelled out here as well."""
    declared = {domain for domain, adapter in ADAPTERS.items() if has_card_media_only(adapter)}
    assert declared == CARD_MEDIA_ONLY


def test_an_empty_media_tuple_is_not_blind_where_media_come_from_the_card() -> None:
    """The 2026-09-19 false alarm: a perfect parse on such a site has no media, every time."""
    parsed = ParseResult(page=PageData(url="u", title="t", duration_s=600, performers=("Who",)))

    counted = adapter_health.outcome_of(parsed)
    ignored = adapter_health.outcome_of(parsed, media_counts=False)

    assert counted.no_media and counted.blind
    # Still recorded — it is context, like duration and performers — but it cannot alarm.
    assert ignored.no_media and not ignored.blind


def test_no_page_and_no_title_still_alarm_where_media_come_from_the_card() -> None:
    """Only the media half is switched off: a real break on such a site is still a break."""
    assert adapter_health.outcome_of(ParseResult(), media_counts=False).blind
    untitled = ParseResult(page=PageData(url="u", title="  "))
    assert adapter_health.outcome_of(untitled, media_counts=False).blind


def test_the_verdict_never_blames_media_a_site_does_not_publish() -> None:
    broken = adapter_health.Window(parses=100, blind=90)
    detail = adapter_health.judge(
        broken, adapter_health.Window(), was_broken=False, media_counts=False
    ).detail
    assert "no media" not in detail and "no page or no title" in detail


@pytest.mark.django_db
def test_which_half_of_the_rule_applies_comes_from_the_registered_adapter(
    site01: Site, site04: Site
) -> None:
    assert adapter_health.media_counts_for(site01)
    assert not adapter_health.media_counts_for(site04)
    # An unregistered domain answers yes: the conservative default alarms rather than stays quiet.
    assert adapter_health.media_counts_for(Site(domain="nobody.invalid"))


@pytest.mark.django_db
def test_a_card_media_site_parsing_everything_but_media_is_healthy(site04: Site) -> None:
    """The regression: 14,212 of 14,212 parses were called blind while every page had its media."""
    now = timezone.now()
    parsed = ParseResult(page=PageData(url="u", title="t", duration_s=600, performers=("Who",)))
    for _ in range(60):
        adapter_health.record_parse(
            site04.pk,
            adapter_health.outcome_of(parsed, media_counts=False),
            now=now,
        )

    report = adapter_health.evaluate(site04, now=now)

    assert report.no_media == 60 and report.recent.blind == 0
    assert report.status == OK and not report.media_counts
    assert AdapterHealth.objects.get(site=site04).status == OK


@pytest.mark.django_db
def test_a_card_media_site_that_stops_titling_is_still_called_broken(site04: Site) -> None:
    now = timezone.now()
    for _ in range(60):
        adapter_health.record_parse(
            site04.pk,
            adapter_health.ParseOutcome(no_page=False, no_title=True, no_media=True),
            now=now,
        )

    report = adapter_health.evaluate(site04, now=now)

    assert report.status == BROKEN and report.recent.blind == 60
    assert "no media" not in report.detail


@pytest.mark.django_db
def test_the_report_marks_a_site_whose_media_come_from_the_card(site04: Site) -> None:
    now = timezone.now()
    for _ in range(40):
        adapter_health.record_parse(
            site04.pk, adapter_health.ParseOutcome(no_page=False, no_media=True), now=now
        )
    text = adapter_health.render_text(adapter_health.evaluate_all(now=now))
    assert "site04.example *" in text and "no media of their own" in text
    assert "look broken" not in text


@pytest.mark.django_db
def test_the_worker_asks_the_adapter_before_counting_a_missing_poster(site04: Site) -> None:
    """Without this the repair's daily cap would be spent on pages nothing is wrong with."""
    from crawler.worker import CrawlWorker

    task = CrawlTask.objects.create(
        site=site04, url="https://site04.example/watch/1", kind=CrawlTask.Kind.DETAIL
    )
    worker = CrawlWorker.__new__(CrawlWorker)
    parsed = ParseResult(page=PageData(url="u", title="t", duration_s=600, performers=("Who",)))

    worker._watch_extraction(task, parsed, "<html></html>", ADAPTERS["site04.example"])

    row = AdapterParseStat.objects.get(site=site04)
    assert (row.parses, row.no_page, row.no_title, row.no_media) == (1, 0, 0, 1)
    assert adapter_health.evaluate(site04, write=False).recent.blind == 0
