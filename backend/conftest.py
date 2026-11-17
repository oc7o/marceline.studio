from collections.abc import Callable

import pytest
from django.utils import timezone
from pytest_django.fixtures import Settings

from catalog.models import Site
from catalog.safety_term_defaults import ensure_default_safety_terms
from crawler import services as crawler_services
from index.models import Media, Page

# Operational switches pinned to their code defaults (marceline/settings.py) for every test.
# `core.env.load_dotenv` copies the real `.env` into `os.environ` before settings are read, so
# without this a test silently depends on how the operator's machine is configured: turning on
# TARGETED_INDEXING_ENABLED there changed the scheduler's job order and broke a test (2026-09-17,
# the same bug as test_safety_alerts.py::test_defaults_are_off). A test that needs another value
# sets it with the `settings` fixture or `override_settings`; both take effect after this.
# Not pinned: EMAIL_* (the test runner swaps every mailer to locmem), BACKUP_ALERT_* (read from
# os.environ at call time; test_backup_alerts.py clears them) and SENTRY_DSN (consumed once at
# settings import, so pinning the setting afterwards would change nothing).
OPERATIONAL_DEFAULTS: dict[str, object] = {
    # Issue #92: the guard on missing Unternehmer evidence. `warn` is the code default; a test that
    # wants the refusal sets `refuse` explicitly, and nobody's `.env` can turn it on under a test.
    "ONBOARDING_BUSINESS_STATUS_GUARD": "warn",
    # Issue #115, ADR 0052: onboarding by invite. Off, and with no published documents — a test
    # that exercises the flow switches both on explicitly, so nobody's `.env` can make a test pass
    # only because the operator happened to have configured a real document version.
    "ONBOARDING_INVITES_ENABLED": False,
    "ONBOARDING_INVITE_EXPIRY_DAYS": 14,
    "ONBOARDING_INVITE_BASE_URL": "",
    "ONBOARDING_DOCUMENTS": "",
    # ADR 0054: the mailed account actions. Off, on the code lifetimes, and with the project's own
    # role addresses — a test that exercises a flow switches the flag on explicitly and asserts
    # against the locmem outbox, so nobody's `.env` can make one send a real mail.
    "ACCOUNT_EMAIL_FLOWS_ENABLED": False,
    "ACCOUNT_MAIL_FROM": "noreply@marceline.studio",
    "ACCOUNT_MAIL_REPLY_TO": "contact@marceline.studio",
    "ACCOUNT_PASSWORD_RESET_TTL_MINUTES": 60,
    "ACCOUNT_VERIFICATION_TTL_HOURS": 48,
    "TARGETED_INDEXING_ENABLED": False,
    # ADR 0057: the deletion journal. Off, so no test ever appends to the operator's real journal
    # next to the nightly dumps; a test that exercises it points the setting at a `tmp_path`.
    "DELETION_JOURNAL_DIR": "",
    "DELETION_JOURNAL_SEGMENT": "deletions",
    "PAGE_RETENTION_ENABLED": False,
    "SAFETY_RETENTION_ENABLED": False,
    "CUSTOMER_DATA_DELETION_ENABLED": False,
    "CUSTOMER_DATA_RETENTION_DAYS": 30,
    # ADR 0048: the research side's retention. Off, and on the code periods — a test that measures
    # a period sets it explicitly, and nobody's `.env` can shorten one under a test.
    "RESEARCH_RETENTION_ENABLED": False,
    "SEARCH_QUERY_RETENTION_DAYS": 30,
    "PROSPECT_RESEARCH_RETENTION_DAYS": 90,
    "CREATOR_NAME_RETENTION_DAYS": 90,
    # ADR 0052 amendment 2026-09-20: the clock for an invite that is never redeemed. Off, and on
    # the code period, for the same reason as the research side above.
    "INVITE_RETENTION_ENABLED": False,
    "ONBOARDING_INVITE_RETENTION_DAYS": 30,
    "DISCOVERY_SCHEDULE_ENABLED": False,
    "PROSPECT_DAILY_SEED_CAP": 4,
    # ADR 0027 amendment 2026-09-17: a customer sees a response history from 5 notices on.
    "HOSTING_HISTORY_MIN_NOTICES": 5,
    # LLM extraction as repair when an adapter breaks (issue #22, ADR 0042): off, and never the
    # operator's real API key or model — a test must not be able to spend money.
    "CRAWLER_LLM_REPAIR_ENABLED": False,
    "OPENROUTER_API_KEY": "",
    "CRAWLER_LLM_MODEL": "",
    "CRAWLER_LLM_REPAIR_DAILY_CALLS_PER_SITE": 50,
    # Tripwire T2 (issue #105): detection is on in production and in tests — it is what the tests
    # are about — but the automatic pause is off unless a test asks for it.
    "ROBOTS_RESERVATION_DETECTION_ENABLED": True,
    "ROBOTS_RESERVATION_PAUSE_ENABLED": False,
    "BROWSER_CAPTURE_ENABLED": False,
    "BROWSER_CAPTURE_WINDOW_UTC": "01:00-05:00",
    "BROWSER_CAPTURE_PER_SITE_PER_DAY": 5,
    "BROWSER_CAPTURE_TOTAL_PER_DAY": 20,
    "BROWSER_CAPTURE_MAX_CONCURRENCY": 1,
    "BROWSER_CAPTURE_ENGINE": "chromium",
    "BROWSER_CAPTURE_EXECUTABLE": "",
    "SAFETY_ALERT_TO": "",
    # Issue #22: a test must not be able to mail the operator about an adapter either.
    "ADAPTER_ALERT_TO": "",
    "ADAPTER_ALERT_FROM": "",
    "SAFETY_ALERT_FROM": "",
    "SAFETY_ALERT_MODERATION_URL": "",
    # 2026-09-19: which event kinds still mail the operator immediately, on top of the daily
    # report. Empty (none) is the default; a test that wants the old per-event mail sets it itself.
    "OPERATOR_MAIL_IMMEDIATE": "",
    # Daily safety digest (crawler/safety_digest.py): the UTC hour the scheduler sends at.
    "SAFETY_DIGEST_HOUR_UTC": 6,
    # Issue #107: the catalogue-share monitoring threshold. Pinned so a test that builds a share
    # asserts against the code default, whatever the operator's `.env` says.
    "CATALOGUE_SHARE_ALERT_PERCENT": 25.0,
    # ADR 0028: a test is development with no production to talk to, unless it says otherwise.
    "MARCELINE_ROLE": "dev",
    "CACHE_SIGNING_KEY_PATH": "",
    "CACHE_PURGE_BASE_URL": "",
    "CACHE_PURGE_OUTBOX": False,
    "CACHE_EXPORT_BASE_URL": "",
    "CACHE_EXPORT_TOKEN": "",
    "CACHE_VERIFY_KEY_PATH": "",
    "OUTBOX_ENABLED": False,
    "OUTBOX_SIGNING_KEY_PATH": "",
    "OUTBOX_VERIFY_KEY_PATH": "",
    "OUTBOX_STALE_ALERT_HOURS": 0.5,
    "OUTBOX_PAUSE_AFTER_HOURS": 6.0,
    # Billing (ADR 0032): the operator's real seller identity must never reach a test or a PDF
    # rendered by one. Billing tests set synthetic values explicitly.
    "BILLING_SELLER_NAME": "",
    "BILLING_SELLER_ADDRESS": "",
    "BILLING_SELLER_TAX_NUMBER": "",
    "BILLING_SELLER_VAT_ID": "",
    "BILLING_SELLER_EMAIL": "",
    "BILLING_SELLER_IBAN": "",
    "BILLING_SELLER_BIC": "",
    "BILLING_SELLER_BANK": "",
    "BILLING_SMALL_BUSINESS": "",
    "BILLING_PAYMENT_TERM_DAYS": 14,
    # Crawl runtime (ADR 0030): which sites it serves and the CDN host delay.
    "RUNTIME_SITES": [],
    "CDN_HOST_DELAY_S": 1.0,
    "BILLING_INVOICE_NUMBER_PREFIX": "",
    # Rights vault (ADR 0034): never the operator's real key or directory; rights tests generate
    # a key and use a temporary directory.
    # Account-control verification (ADR 0055): off, as in production. A test that exercises the
    # flow switches it on explicitly.
    "ACCOUNT_CONTROL_VERIFICATION_ENABLED": False,
    # The creator's own data requests (ADR 0056, issue #133): off, as in production. A test that
    # exercises the two requests switches it on explicitly.
    "CUSTOMER_DATA_REQUESTS_ENABLED": False,
    "RIGHTS_VAULT_KEYS": "",
    "RIGHTS_VAULT_DIR": "",
    "RIGHTS_PROOF_MAX_BYTES": 10 * 1024 * 1024,
    "PROVIDER_CHANNEL_RECHECK_DAYS": 90,
    # Cloud fingerprinting (issue #59, ADR 0044): off, as in production, and never the operator's
    # real spool directory — a test that needs the feature points it at `tmp_path`.
    "CLOUD_FINGERPRINTING_ENABLED": False,
    "CLOUD_UPLOAD_SPOOL_DIR": "",
    "CLOUD_UPLOAD_CHUNK_BYTES": 8 * 1024 * 1024,
    "CLOUD_UPLOAD_MAX_BYTES": 10 * 1024 * 1024 * 1024,
    "CLOUD_UPLOAD_SPOOL_MAX_BYTES": 40 * 1024 * 1024 * 1024,
    "CLOUD_UPLOAD_DAILY_BYTES_PER_ORG": 60 * 1024 * 1024 * 1024,
    "CLOUD_UPLOAD_TTL_HOURS": 6.0,
    # The streaming relay (ADR 0044 amendment 2026-09-19): off, and with no credential, unless a
    # test says otherwise.
    "CLOUD_UPLOAD_RELAY_MODE": False,
    "CLOUD_RELAY_BASE_URL": "",
    "CLOUD_RELAY_TOKEN": "",
    # Notice drafts (ADR 0035): never the operator's real sandbox mailbox in a test.
    "NOTICE_AVAILABILITY_MAX_AGE_HOURS": 48,
    "NOTICE_SANDBOX_RECIPIENTS": [],
    "NOTICE_SANDBOX_FROM": "",
    # Black-bar removal before hashing (docs/matching/black-bars.md): off, as in production.
    "MATCHING_CROP_BLACK_BARS": False,
    # Frame embeddings (ADR 0037): off, as in production, and never the operator's model path.
    # Tests that need the signal set the flag themselves and inject a fake encoder.
    "MATCHING_EMBEDDINGS_ENABLED": False,
    "MATCHING_EMBEDDING_MODEL_PATH": "",
    "MATCHING_EMBEDDING_TOP_K": 10,
    # The reference-side crop grid (ADR 0039): off, as in production. A test that needs the signal
    # turns it on itself; storing a grid needs no flag, only consulting one does.
    "MATCHING_REFERENCE_CROP_GRID": False,
    # Metadata scoring (ADR 0040): off, as in production. Tests that need the signal set the flag
    # themselves; the pure scorer needs no flag at all.
    "MATCHING_METADATA_CANDIDATES_ENABLED": False,
    # The do-not-contact record (ADR 0047): never the operator's real key, which is the one thing
    # that could turn a test digest into something comparable with a real recorded objection. A
    # test that needs a key passes its own, or sets this one.
    "DO_NOT_CONTACT_HMAC_KEY": "",
}


@pytest.fixture(autouse=True)
def _clear_crawler_reference_caches() -> None:
    # crawler.services caches safety terms and creators per process; rows of an earlier test's
    # rolled-back transaction must not leak into this one.
    crawler_services.clear_reference_caches()


@pytest.fixture(autouse=True)
def _pin_operational_settings(settings: Settings) -> None:
    for name, value in OPERATIONAL_DEFAULTS.items():
        setattr(settings, name, value)


@pytest.fixture(autouse=True)
def _ensure_safety_terms(request: pytest.FixtureRequest) -> None:
    """Restore the 34 starter `catalog.SafetyTerm` rows before any test that touches the
    database (`catalog/safety_term_defaults.py`).

    Those rows are seeded once, by a data migration, when the test database is built — a
    session-scoped load. A `django_db(transaction=True)` test flushes every table in its
    teardown, the migration never runs again, and without this the rows are simply gone for
    every test that runs afterwards in the same worker, in whatever order pytest happens to run
    them.

    "Does this test use the database" is decided the same way pytest-django itself decides it
    (`_get_databases_for_test` in `pytest_django/fixtures.py`): the `django_db` marker, or one of
    `db` / `transactional_db` / `live_server` in its *static* fixture closure. The marker check
    matters on its own — pytest-django applies the marker via a dynamic `getfixturevalue` call in
    an autouse fixture of its own, so a marker-only test (`pytestmark = pytest.mark.django_db`,
    the majority idiom here) never has `db`/`transactional_db` in that static closure at all, and
    whether it has been resolved dynamically yet depends on fixture execution order, which is not
    guaranteed between two independent autouse fixtures. Checking the marker directly is
    order-independent; checking the three fixture names covers a test that instead requests `db`
    or `transactional_db` directly (or, transitively, this file's own `site`/`make_media`).
    A test that needs none of that pays for no query at all.
    """
    marker = request.node.get_closest_marker("django_db")
    uses_db = (
        marker is not None
        or "db" in request.fixturenames
        or "transactional_db" in request.fixturenames
        or "live_server" in request.fixturenames
    )
    if not uses_db:
        return
    # `request.getfixturevalue` is idempotent per test (function-scoped fixtures are cached), so
    # this is safe whether or not something else already triggered `db`'s setup — it only needs
    # to have happened, in either order, before we query below.
    request.getfixturevalue("db")
    ensure_default_safety_terms()


@pytest.fixture
def site(db: None) -> Site:
    return Site.objects.create(domain="example.test", name="Example")


@pytest.fixture
def make_page(site: Site) -> Callable[[str], Page]:
    """A normal indexed page: clear, and scored clearly safe (ADR 0018).

    Pages that were never scored have no fetchable media, so tests that want the ordinary case
    need a score. Use `safety_score=None` explicitly to test an unscored page.
    """

    def _make(slug: str) -> Page:
        return Page.objects.create(
            site=site, url=f"https://example.test/{slug}", title=slug, safety_score=1.0
        )

    return _make


@pytest.fixture
def make_media(db: None) -> Callable[..., Media]:
    """Media that is eligible to fetch, i.e. its page was parsed and scored (ADR 0018)."""

    def _make(
        page: Page,
        url: str = "https://cdn.example.test/poster.png",
        kind: Media.Kind = Media.Kind.POSTER,
        *,
        eligible: bool = True,
    ) -> Media:
        return Media.objects.create(
            page=page,
            kind=kind,
            url=url,
            eligible_at=timezone.now() if eligible else None,
        )

    return _make
