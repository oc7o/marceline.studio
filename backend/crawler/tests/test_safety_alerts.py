"""Active safety notification (issue #40): docs/safety.md, crawler/alerts.py."""

from datetime import timedelta

import pytest
from django.core import mail
from django.db import transaction
from django.utils import timezone
from pytest_django.fixtures import DjangoCaptureOnCommitCallbacks, Settings

from catalog.models import Site
from crawler import alerts, services
from crawler.models import CrawlTask
from crawler.sites.base import MediaSpec, PageData, ParseResult
from index.models import Media, Page

DETAIL = CrawlTask.Kind.DETAIL
VIDEO_URL = "https://site01.example/video/1/x/"
IDENTIFYING_TITLE = "a very identifying scene title"
CaptureCallbacks = DjangoCaptureOnCommitCallbacks
# Django's test environment (`setup_test_environment`, run once per session by pytest-django)
# always swaps every MAILERS alias's BACKEND to locmem, so tests never need to set it themselves.


@pytest.fixture
def site01(db: None) -> Site:
    return Site.objects.create(domain="site01.example", crawl_delay_s=0)


def detail_task(site: Site) -> CrawlTask:
    return CrawlTask.objects.create(site=site, url=VIDEO_URL, kind=DETAIL)


def blocking_result(title: str = "scene") -> ParseResult:
    """A page whose title/tags match the seeded "jailbait" (minor) block term."""
    return ParseResult(
        page=PageData(url=VIDEO_URL, title=title, tags=("jailbait",)),
        links=(),
        media=(MediaSpec(Media.Kind.POSTER, "https://cdn.test/full.jpg"),),
    )


def configure_alerts(
    settings: Settings, *, to: str = "ops@example.test", sender: str = "marceline@example.test"
) -> None:
    """Address the mail **and** switch the immediate incident alert back on.

    Since 2026-09-19 an incident-rule block folds into the daily report by default
    (`OPERATOR_MAIL_IMMEDIATE` empty); this helper is used by the tests below that exercise the
    still-supported immediate path (batching, content rule, failure handling). See
    `test_no_immediate_mail_by_default_even_when_addressed` for the new default.
    """
    settings.SAFETY_ALERT_TO = to
    settings.SAFETY_ALERT_FROM = sender
    settings.OPERATOR_MAIL_IMMEDIATE = "safety_incident"


def blocked_page(site: Site, slug: str, *, term: str = "jailbait", category: str = "minor") -> Page:
    return Page.objects.create(
        site=site,
        url=f"https://example.test/{slug}",
        title=f"title for {slug}",
        safety_status=Page.SafetyStatus.BLOCKED,
        safety_matches=[{"term": term, "category": category, "action": "block"}],
    )


def test_defaults_are_off(site01: Site, settings: Settings) -> None:
    # Django's test environment always swaps the mailer to locmem (that is what makes the
    # mail.outbox tests below possible without a network), so the console-backend default in
    # marceline/settings.py ("no EMAIL_BACKEND set" -> console, which never opens a connection)
    # cannot be exercised through mail.outbox here. What the "off by default" contract needs is
    # that nothing is *addressed*: no recipient/sender configured means nothing is sent.
    #
    # The unconfigured state has to be set explicitly rather than read from the ambient
    # environment. `core.env.load_dotenv` puts the real `.env` into `os.environ`, so on the
    # operator's machine these settings carry their real values and asserting them empty only
    # tested "the operator has not set up mail yet" — which stopped being true on 2026-09-16.
    settings.SAFETY_ALERT_TO = ""
    settings.SAFETY_ALERT_FROM = ""

    page = services.store_parse_result(detail_task(site01), blocking_result())

    assert page is not None and page.safety_status == Page.SafetyStatus.BLOCKED
    assert mail.outbox == []


def test_mail_sent_on_a_new_block(
    site01: Site, settings: Settings, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    configure_alerts(settings)

    with django_capture_on_commit_callbacks(execute=True):
        page = services.store_parse_result(detail_task(site01), blocking_result())

    assert page is not None and page.safety_status == Page.SafetyStatus.BLOCKED
    [sent] = mail.outbox
    assert sent.from_email == "marceline@example.test"
    assert sent.to == ["ops@example.test"]
    assert f"page {page.pk}" in sent.body
    assert "site01.example" in sent.body
    assert "minor" in sent.body
    assert "jailbait" in sent.body
    assert f"/admin/index/page/{page.pk}/change/" in sent.body


def test_mail_waits_until_the_block_is_committed(
    site01: Site, settings: Settings, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    """Sending inside the store transaction held the page's row lock for as long as the mail
    server took, and mailed about blocks that were then rolled back."""
    configure_alerts(settings)

    with django_capture_on_commit_callbacks(execute=True):
        page = services.store_parse_result(detail_task(site01), blocking_result())
        assert page is not None and page.safety_status == Page.SafetyStatus.BLOCKED
        assert mail.outbox == []  # the transaction is still open

    assert len(mail.outbox) == 1


def test_no_mail_for_a_block_that_rolled_back(
    site01: Site, settings: Settings, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    configure_alerts(settings)

    with (
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        services.store_parse_result(detail_task(site01), blocking_result())
        raise RuntimeError("the crawl failed after storing the page")

    assert mail.outbox == []
    assert not Page.objects.exists()


def test_no_mail_when_unconfigured(site01: Site, settings: Settings) -> None:
    settings.SAFETY_ALERT_TO = ""
    settings.SAFETY_ALERT_FROM = ""

    page = services.store_parse_result(detail_task(site01), blocking_result())

    assert page is not None and page.safety_status == Page.SafetyStatus.BLOCKED
    assert mail.outbox == []


def test_no_mail_when_only_one_of_to_from_is_set(site01: Site, settings: Settings) -> None:
    settings.SAFETY_ALERT_TO = "ops@example.test"
    settings.SAFETY_ALERT_FROM = ""

    services.store_parse_result(detail_task(site01), blocking_result())

    assert mail.outbox == []


def test_mail_never_contains_the_page_url_or_title(
    site01: Site, settings: Settings, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    configure_alerts(settings)

    with django_capture_on_commit_callbacks(execute=True):
        page = services.store_parse_result(
            detail_task(site01), blocking_result(title=IDENTIFYING_TITLE)
        )

    assert page is not None
    [sent] = mail.outbox
    assert VIDEO_URL not in sent.body and VIDEO_URL not in sent.subject
    assert IDENTIFYING_TITLE not in sent.body and IDENTIFYING_TITLE not in sent.subject


def tagged_result(tag: str) -> ParseResult:
    return ParseResult(
        page=PageData(url=VIDEO_URL, title="scene", tags=(tag,)),
        links=(),
        media=(MediaSpec(Media.Kind.POSTER, "https://cdn.test/full.jpg"),),
    )


@pytest.mark.parametrize("tag", ("forced", "flagged term"))
def test_an_ordinary_block_waits_for_the_daily_digest(
    tag: str,
    site01: Site,
    settings: Settings,
    django_capture_on_commit_callbacks: CaptureCallbacks,
) -> None:
    """2026-09-18: only a rule that opens a safety incident still mails at once.

    `forced` is `auto_block` (non-consent wording) and `flagged term` is `customer_release_queue`.
    Both block the page, both have their own queue, and neither is worth an interrupt; they go
    into the daily digest instead (crawler/safety_digest.py).
    """
    configure_alerts(settings)

    with django_capture_on_commit_callbacks(execute=True):
        page = services.store_parse_result(detail_task(site01), tagged_result(tag))

    assert page is not None and page.safety_status == Page.SafetyStatus.BLOCKED
    assert mail.outbox == []


def test_no_immediate_mail_by_default_even_when_addressed(site: Site, settings: Settings) -> None:
    """2026-09-19: an incident-rule block no longer mails on its own by default.

    Addresses configured but `OPERATOR_MAIL_IMMEDIATE` left at its default (empty) — the block
    still happens and the page is still `blocked`, but nothing is sent; the daily report lists it
    instead (`crawler/tests/test_safety_digest.py`).
    """
    settings.SAFETY_ALERT_TO = "ops@example.test"
    settings.SAFETY_ALERT_FROM = "marceline@example.test"
    settings.OPERATOR_MAIL_IMMEDIATE = ""
    incident = blocked_page(site, "incident", term="jailbait", category="minor")
    incident.safety_matches = [
        {"term": "jailbait", "category": "minor", "action": "auto_block_and_incident"}
    ]

    alerts.notify_block(incident)

    assert mail.outbox == []


def test_immediate_mail_names_only_the_configured_kind(site: Site, settings: Settings) -> None:
    """OPERATOR_MAIL_IMMEDIATE is a comma list of `core.operator_mail.KINDS`; an unrelated kind
    named in it must not turn the incident alert back on."""
    settings.SAFETY_ALERT_TO = "ops@example.test"
    settings.SAFETY_ALERT_FROM = "marceline@example.test"
    settings.OPERATOR_MAIL_IMMEDIATE = "tdm_reservation,adapter_health"
    incident = blocked_page(site, "incident", term="jailbait", category="minor")
    incident.safety_matches = [
        {"term": "jailbait", "category": "minor", "action": "auto_block_and_incident"}
    ]

    alerts.notify_block(incident)

    assert mail.outbox == []


def test_an_incident_rule_block_still_mails_at_once(site: Site, settings: Settings) -> None:
    configure_alerts(settings)
    ordinary = blocked_page(site, "ordinary", term="forced", category="non_consent")
    ordinary.safety_matches = [
        {"term": "forced", "category": "non_consent", "action": "auto_block"}
    ]
    incident = blocked_page(site, "incident", term="jailbait", category="minor")
    incident.safety_matches = [
        {"term": "jailbait", "category": "minor", "action": "auto_block_and_incident"}
    ]

    alerts.notify_block(ordinary)
    assert mail.outbox == []

    alerts.notify_block(incident)
    [sent] = mail.outbox
    assert f"page {incident.pk}" in sent.body
    assert f"page {ordinary.pk}" not in sent.body
    assert "incident rule" in sent.subject


def test_review_status_never_mails(site01: Site, settings: Settings) -> None:
    """Only `blocked` mails (docs/safety.md); `review` pages just wait in the moderation queue."""
    configure_alerts(settings)
    result = ParseResult(page=PageData(url=VIDEO_URL, title="hidden cam scene"))

    page = services.store_parse_result(detail_task(site01), result)

    assert page is not None and page.safety_status == Page.SafetyStatus.REVIEW
    assert mail.outbox == []


def test_batches_pages_blocked_within_the_interval(site: Site, settings: Settings) -> None:
    configure_alerts(settings)
    settings.SAFETY_ALERT_INTERVAL_MINUTES = 10
    now = timezone.now()
    first = blocked_page(site, "one", term="jailbait", category="minor")
    second = blocked_page(site, "two", term="forced", category="non_consent")

    alerts.notify_block(first, now=now)
    alerts.notify_block(second, now=now + timedelta(minutes=1))

    # The first block (nothing sent yet) goes out immediately; the second, inside the same
    # interval, is queued rather than mailed on its own.
    [sent] = mail.outbox
    assert f"page {first.pk}" in sent.body
    assert f"page {second.pk}" not in sent.body

    third = blocked_page(site, "three")
    alerts.notify_block(third, now=now + timedelta(minutes=2))
    assert len(mail.outbox) == 1  # still inside the interval

    alerts.notify_block(third, now=now + timedelta(minutes=11))
    assert len(mail.outbox) == 2
    combined_second_mail = mail.outbox[1].body
    assert f"page {second.pk}" in combined_second_mail
    assert f"page {third.pk}" in combined_second_mail


def test_flush_pending_forces_a_send_ignoring_the_interval(site: Site, settings: Settings) -> None:
    configure_alerts(settings)
    now = timezone.now()
    first = blocked_page(site, "one")
    alerts.notify_block(first, now=now)
    assert len(mail.outbox) == 1

    second = blocked_page(site, "two")
    alerts.notify_block(second, now=now + timedelta(seconds=1))
    assert len(mail.outbox) == 1  # queued, interval not elapsed

    alerts.flush_pending(now=now + timedelta(seconds=2))
    assert len(mail.outbox) == 2
    assert f"page {second.pk}" in mail.outbox[1].body


def test_scan_pages_flushes_a_pending_batch_before_returning(
    site: Site, settings: Settings
) -> None:
    """`safety_scan` is a one-shot process: a queued-but-not-yet-due batch must still go out
    before the command exits, or it is lost until some later run (crawler/alerts.py)."""
    configure_alerts(settings)
    first = Page.objects.create(site=site, url="https://example.test/1", title="jailbait one")
    second = Page.objects.create(site=site, url="https://example.test/2", title="jailbait two")

    services.scan_pages()

    assert len(mail.outbox) <= 2  # batched, never one mail per page
    combined = "\n".join(str(sent.body) for sent in mail.outbox)
    assert f"page {first.pk}" in combined
    assert f"page {second.pk}" in combined
    assert "jailbait one" not in combined and "jailbait two" not in combined


def test_a_send_failure_is_logged_and_does_not_break_the_crawl(
    site01: Site,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    django_capture_on_commit_callbacks: CaptureCallbacks,
) -> None:
    configure_alerts(settings)

    class BoomingMessage:
        def __init__(self, **kwargs: object) -> None:
            pass

        def send(self, **kwargs: object) -> int:
            raise RuntimeError("smtp exploded")

    monkeypatch.setattr(alerts, "EmailMessage", BoomingMessage)

    with caplog.at_level("ERROR"), django_capture_on_commit_callbacks(execute=True):
        page = services.store_parse_result(detail_task(site01), blocking_result())

    assert page is not None and page.safety_status == Page.SafetyStatus.BLOCKED
    assert "safety alert" in caplog.text.lower()


def test_dry_run_scan_never_mails(site: Site, settings: Settings) -> None:
    configure_alerts(settings)
    Page.objects.create(site=site, url="https://example.test/1", title="jailbait scene")

    services.scan_pages(dry_run=True)

    assert mail.outbox == []
