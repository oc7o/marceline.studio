"""Django settings. All configuration comes from environment variables (see /.env.example)."""

from pathlib import Path

import django_stubs_ext
from django.core.exceptions import ImproperlyConfigured

from core.deploy import (
    PUBLIC_SURFACES,
    CloudUploadSettings,
    ProductionSettings,
    cloud_upload_problems,
    production_problems,
    role_problems,
)
from core.env import env_bool, env_float, env_int, env_is_set, env_list, env_str, load_dotenv
from core.sentry import init_sentry

django_stubs_ext.monkeypatch()

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent
load_dotenv(REPO_ROOT / ".env")

# "dev" gives safe local defaults; "prod" requires every secret to be set explicitly.
DJANGO_ENV = env_str("DJANGO_ENV", default="dev")
IS_PROD = DJANGO_ENV == "prod"

# Never in production. Development may switch it off for a listener another machine reaches (the
# ADR 0028 export listener), so an error there is a bare 500 instead of a traceback.
DEBUG = False if IS_PROD else env_bool("DJANGO_DEBUG", default=True)
SECRET_KEY = env_str("DJANGO_SECRET_KEY", default=None if IS_PROD else "dev-insecure-secret-key")
ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS", default=[] if IS_PROD else ["localhost", "127.0.0.1", "0.0.0.0"]
)

# What this process answers (ADR 0028, marceline/public_surface.py): `full` = staff review API,
# admin and customer API (development / internal VPS, reached over Tailscale only); `customer` =
# the public production cache, where everything but the customer API and the health check is 404;
# `export` = development's Tailscale-only export listener (the export API and nothing else);
# `internal` = production's Tailscale-only purge listener (the purge endpoint and nothing else).
# Production has no default: putting the staff surface on the internet must be written down.
PUBLIC_SURFACE = env_str("DJANGO_PUBLIC_SURFACE", default="" if IS_PROD else "full")
if PUBLIC_SURFACE and PUBLIC_SURFACE not in PUBLIC_SURFACES:
    raise ImproperlyConfigured(f"DJANGO_PUBLIC_SURFACE must be one of {', '.join(PUBLIC_SURFACES)}")

# Which machine of ADR 0028 this process belongs to. `dev`: the main database; serves the export
# API, sends purges, and answers customer reads from its own tables. `prod`: the read-through
# cache; answers customer reads from signed bundles (marceline/customer_cache.py), receives purges,
# and serves no export. Defaults follow DJANGO_ENV; a surface that belongs to the other machine
# refuses to start.
MARCELINE_ROLE = env_str("MARCELINE_ROLE", default="prod" if IS_PROD else "dev")
_role_problems = role_problems(MARCELINE_ROLE, PUBLIC_SURFACE or None, django_env=DJANGO_ENV)
if _role_problems:
    raise ImproperlyConfigured("; ".join(_role_problems))

# The cache between the two machines (ADR 0028, docs/runbooks/production.md "Data"). Key files live
# outside the repository and are generated with `manage.py generate_signing_key`; only paths are
# configured here, never key material. Empty values switch the respective part off.
# Development: the Ed25519 private key that signs bundles, accounts and purges, and production's
# Tailscale-only purge listener (e.g. http://prod.example.internal:8081).
CACHE_SIGNING_KEY_PATH = env_str("CACHE_SIGNING_KEY_PATH", default="")
CACHE_PURGE_BASE_URL = env_str("CACHE_PURGE_BASE_URL", default="")
# Record purges without sending them (a development process without the key, e.g. `web`);
# `manage.py send_cache_purges` in a process with the key and the URL delivers them.
CACHE_PURGE_OUTBOX = env_bool("CACHE_PURGE_OUTBOX", default=False)
# Production: development's export listener over Tailscale, the scoped token for it
# (`manage.py create_service_token` on development), and development's public key.
CACHE_EXPORT_BASE_URL = env_str("CACHE_EXPORT_BASE_URL", default="")
CACHE_EXPORT_TOKEN = env_str("CACHE_EXPORT_TOKEN", default="")
CACHE_VERIFY_KEY_PATH = env_str("CACHE_VERIFY_KEY_PATH", default="")

# Customer writes on production (ADR 0028 amendment "The production write path"): production
# accepts them into an outbox, development pulls and applies them. Production: `OUTBOX_ENABLED`
# accepts writes (and makes `me.can_add_originals` true); the `internal` listener signs the batches
# it hands out with production's own Ed25519 key (`OUTBOX_SIGNING_KEY_PATH`, never on the public
# `web` process). Development: production's public key (`OUTBOX_VERIFY_KEY_PATH`); the pull goes to
# `CACHE_PURGE_BASE_URL`, production's Tailscale-only listener, signed with the key above.
OUTBOX_ENABLED = env_bool("OUTBOX_ENABLED", default=False)
OUTBOX_SIGNING_KEY_PATH = env_str("OUTBOX_SIGNING_KEY_PATH", default="")
OUTBOX_VERIFY_KEY_PATH = env_str("OUTBOX_VERIFY_KEY_PATH", default="")
# A write still pending after this many hours is reported as an error (Sentry), once per item.
# Default 0.5 h (30 min, operator decision 2026-09-17).
OUTBOX_STALE_ALERT_HOURS = env_float("OUTBOX_STALE_ALERT_HOURS", default=0.5)
# Development has not pulled for this many hours: production refuses new writes (503). Default 6 h
# (operator decision 2026-09-17).
OUTBOX_PAUSE_AFTER_HOURS = env_float("OUTBOX_PAUSE_AFTER_HOURS", default=6.0)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "ninja",
    "core",
    "accounts",
    "billing",
    "catalog",
    "index",
    "crawler",
    "matching",
    "delivery",
    # The project package is an app for cross-app management commands (`scheduler`), the page
    # signals that queue production cache purges (ADR 0028) and the one table that joins apps no
    # contract lets meet: original ownership (ADR 0031).
    "marceline",
]

# Worker and scheduler log lines (INFO) go to stdout; Django keeps its own logger config.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {"marceline": {"handlers": ["console"], "level": "INFO"}},
}

MIDDLEWARE = [
    # Outermost: a refused path gets the same 404 over HTTP and HTTPS (no redirect that differs
    # from an unknown path) and never touches the session store or the database.
    "marceline.public_surface.PublicSurfaceMiddleware",
    # Above everything that answers, so it also covers the production cache's bundle answers: a
    # customer API response is for one account and no cache may store it.
    "marceline.no_store.CustomerNoStoreMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Innermost: on production (MARCELINE_ROLE=prod) customer data reads are answered from the
    # signed cache, after authentication and with every outer middleware still applied.
    "marceline.customer_cache.CustomerCacheMiddleware",
]

ROOT_URLCONF = "marceline.urls"
WSGI_APPLICATION = "marceline.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": env_str("POSTGRES_HOST", default="localhost"),
        "PORT": env_int("POSTGRES_PORT", default=5433),
        "NAME": env_str("POSTGRES_DB", default="marceline"),
        "USER": env_str("POSTGRES_USER", default="marceline"),
        "PASSWORD": env_str("POSTGRES_PASSWORD", default=None if IS_PROD else "marceline"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Session cookies (ADR 0023). One session mechanism serves the staff SPA and the customer product,
# so these flags are the whole of "is the cookie safe in production". Secure is on whenever
# DJANGO_ENV=prod (Caddy terminates TLS, ADR 0005) and can be forced on in dev over HTTPS; leaving
# it off in prod would put the session cookie on the wire in clear. SameSite=Lax is enough because
# both SPAs are served from the same origin as the API, and the CSRF token is checked on every
# unsafe request anyway.
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", default=IS_PROD)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = env_int("SESSION_COOKIE_AGE_SECONDS", default=14 * 24 * 60 * 60)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", default=IS_PROD)
# The CSRF cookie is read by JavaScript on purpose: the SPA echoes it as X-CSRFToken. It is not a
# credential — the session cookie is, and that one stays HttpOnly.
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
# Needed once the site is served over HTTPS under a real hostname, e.g.
# https://dash.marceline.studio — Django checks every unsafe request's Origin against this list.
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# Login rate limiting and lockout (ADR 0023, accounts/login_throttle.py). Shared by the staff and
# the customer login. Defaults: five failed attempts per account and twenty per IP address inside a
# fifteen-minute window, then fifteen minutes of refusal counted from the last failure. The bucket
# is keyed by the submitted username, so an account that does not exist locks out exactly like one
# that does.
LOGIN_MAX_ACCOUNT_ATTEMPTS = env_int("LOGIN_MAX_ACCOUNT_ATTEMPTS", default=5)
LOGIN_MAX_IP_ATTEMPTS = env_int("LOGIN_MAX_IP_ATTEMPTS", default=20)
LOGIN_ATTEMPT_WINDOW_MINUTES = env_int("LOGIN_ATTEMPT_WINDOW_MINUTES", default=15)
LOGIN_LOCKOUT_MINUTES = env_int("LOGIN_LOCKOUT_MINUTES", default=15)
# How many reverse proxies sit in front of Django. 0 (the default) counts REMOTE_ADDR and ignores
# X-Forwarded-For entirely; behind Caddy set 1, so the per-IP limit counts the visitor rather than
# the proxy. Setting it higher than the real number of proxies lets a client forge its own address.
LOGIN_TRUSTED_PROXIES = env_int("LOGIN_TRUSTED_PROXIES", default=0)

# HTTPS behind Caddy (ADR 0005, ADR 0028). Caddy terminates TLS and overwrites X-Forwarded-Proto
# with the scheme it actually received, so the header is trustworthy exactly when Django is only
# reachable through Caddy — which is why production refuses to start without a proxy count.
# Development keeps plain HTTP and no HSTS: a browser that once saw HSTS for localhost keeps it.
if IS_PROD:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", default=IS_PROD)
# The container health check calls gunicorn directly over plain HTTP. So does development's purge,
# over Tailscale (WireGuard-encrypted, and the purge itself is signed; no cookie is involved) to
# production's internal listener (ADR 0028) — and, since the write-path amendment, development's
# pull/ack of the customer write outbox on the same plain-HTTP listener (signed envelopes, no
# cookie either).
SECURE_REDIRECT_EXEMPT = [
    r"^api/v1/health$",
    r"^internal/v1/cache/purge$",
    r"^internal/v1/outbox/pull$",
    r"^internal/v1/outbox/ack$",
]
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", default=365 * 24 * 60 * 60 if IS_PROD else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=IS_PROD)
# Preloading commits the whole registrable domain for months; an operator decision, never a default.
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", default=False)
SECURE_CONTENT_TYPE_NOSNIFF = True
# No Referer leaves our pages (frontend/CLAUDE.md privacy rule).
SECURE_REFERRER_POLICY = "no-referrer"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

if IS_PROD:
    _problems = production_problems(
        ProductionSettings(
            secret_key=SECRET_KEY,
            allowed_hosts=ALLOWED_HOSTS,
            csrf_trusted_origins=CSRF_TRUSTED_ORIGINS,
            session_cookie_secure=SESSION_COOKIE_SECURE,
            csrf_cookie_secure=CSRF_COOKIE_SECURE,
            ssl_redirect=SECURE_SSL_REDIRECT,
            hsts_seconds=SECURE_HSTS_SECONDS,
            trusted_proxies=LOGIN_TRUSTED_PROXIES,
            database_password=env_str("POSTGRES_PASSWORD", default=""),
            public_surface=PUBLIC_SURFACE or None,
        )
    )
    if _problems:
        raise ImproperlyConfigured("Unsafe production settings: " + "; ".join(_problems))

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Crawler: product token only during development, no contact address.
CRAWLER_USER_AGENT = env_str("CRAWLER_USER_AGENT", default="MarcelineBot/0.1")

# Crawl runtime (ADR 0030, issue #75): `crawl_runtime --lane pages|media`.
# Which sites the runtime serves: comma-separated domains, or `all` (every enabled site). Empty
# refuses to start, so a missing value can never take over every site by accident. A site whose
# legacy per-site worker still runs is skipped either way (per-site advisory locks).
RUNTIME_SITES = env_list("RUNTIME_SITES", default=[])
# A CDN host's own slot: at least this many seconds between two requests to it, across every site
# and process (a robots.txt Crawl-delay or the slot's admin floor can raise it). Origin hosts keep
# the site's delay, shared by pages, robots.txt and origin media.
CDN_HOST_DELAY_S = env_float("CDN_HOST_DELAY_S", default=1.0)

RUNTIME_PAGE_THREADS = env_int("RUNTIME_PAGE_THREADS", default=6)
RUNTIME_MEDIA_THREADS = env_int("RUNTIME_MEDIA_THREADS", default=8)
RUNTIME_HASH_PROCESSES = env_int("RUNTIME_HASH_PROCESSES", default=2)
# A thread waits at most this long for a host slot, then hands its task back (attempt refunded).
RUNTIME_SLOT_MAX_WAIT_S = env_float("RUNTIME_SLOT_MAX_WAIT_S", default=150.0)
# Whole-response deadlines: a server dripping bytes cannot hold a thread (or a host's lease).
RUNTIME_PAGE_DEADLINE_S = env_float("RUNTIME_PAGE_DEADLINE_S", default=60.0)
RUNTIME_MEDIA_DEADLINE_S = env_float("RUNTIME_MEDIA_DEADLINE_S", default=120.0)
# Graceful self-restart (compose starts a fresh process): after this many tasks, above this RSS
# (the lane process itself, hash children not included) or after this many hours.
RUNTIME_MAX_TASKS = env_int("RUNTIME_MAX_TASKS", default=20_000)
RUNTIME_MAX_RSS_MB = env_int("RUNTIME_MAX_RSS_MB", default=500)
RUNTIME_MAX_UPTIME_H = env_float("RUNTIME_MAX_UPTIME_H", default=24.0)

# Dead media URL shapes (ADR 0042, issue #82). A site whose theme advertises a poster it never
# generated answers 404 for every page we index; the breaker stops fetching that URL *shape* on
# that site once it has answered 404 often enough, and probes it once per backoff window so a
# repaired theme is never locked out. OFF by default: the counters are kept either way, so the
# operator reads `request_report` on live traffic before handing it the budget. It suppresses only
# on 404/410 - never a 5xx, a 429, a 403, an expired token or anything refused before it was sent.
MEDIA_URL_BREAKER_ENABLED = env_bool("MEDIA_URL_BREAKER_ENABLED", default=False)
# Consecutive 404/410 answers before a shape may be suppressed. 20 is measured, not taste: a
# *healthy* shape in the live index produced a run of 51 (ADR 0042 s 3), which is why the streak
# alone never decides - the share below has to agree.
MEDIA_SHAPE_GONE_STREAK = env_int("MEDIA_SHAPE_GONE_STREAK", default=20)
# ... and this much of the shape's recent settled answers must be 404/410 as well.
MEDIA_SHAPE_GONE_SHARE = env_float("MEDIA_SHAPE_GONE_SHARE", default=0.70)
# The decayed window those shares are taken over: both counters halve once their sum passes it.
MEDIA_SHAPE_WINDOW = env_int("MEDIA_SHAPE_WINDOW", default=100)
# A streak whose last 404 is older than this starts again (hours).
MEDIA_SHAPE_STREAK_TTL_H = env_float("MEDIA_SHAPE_STREAK_TTL_H", default=24.0 * 7)
# Suppression: the first window, then doubling per re-trip, capped. A suppressed shape costs one
# probe request per window, so there is nothing to buy by waiting longer than a few days.
MEDIA_SHAPE_BACKOFF_H = env_float("MEDIA_SHAPE_BACKOFF_H", default=6.0)
MEDIA_SHAPE_MAX_BACKOFF_H = env_float("MEDIA_SHAPE_MAX_BACKOFF_H", default=72.0)
# How long one half-open probe holds the shape before the next claimant takes over (minutes).
MEDIA_SHAPE_PROBE_TIMEOUT_MIN = env_float("MEDIA_SHAPE_PROBE_TIMEOUT_MIN", default=10.0)

# Machine-readable TDM/AI reservations in robots.txt (tripwire T2, issue #105). Detection is ON by
# default and costs nothing: it reads the robots.txt the crawler already fetches once per origin
# per 24 h, stores `catalog.SiteRobotsPolicy` (agent tokens, booleans, a content hash — never a path
# or a line of the file) and alerts the operator once per new or changed reservation.
ROBOTS_RESERVATION_DETECTION_ENABLED = env_bool(
    "ROBOTS_RESERVATION_DETECTION_ENABLED", default=True
)
# The automatic pause is OFF by default: detection is harmless, pausing changes what we crawl. With
# it on, a site whose robots.txt shuts our **own** product token out of the whole host, or whose
# robots.txt response carries a `TDM-Reservation` / `X-Robots-Tag: noai` header, is paused for
# `catalog.robots_policy.PAUSE_DAYS` with `pause_reason` `tdm_reservation` and waits for the
# operator. A reservation addressed only to other companies' AI tokens never pauses anything, and a
# reservation naming `*` needs no pause: the ordinary robots.txt logic already skips every URL.
ROBOTS_RESERVATION_PAUSE_ENABLED = env_bool("ROBOTS_RESERVATION_PAUSE_ENABLED", default=False)

# Customer case file (ADR 0025): reconcile an original's case whenever a customer reads it.
# ADR 0028 turns this off where the API must not write on read; `manage.py sync_cases` then keeps
# the cases current.
CASE_SYNC_ON_READ = env_bool("CASE_SYNC_ON_READ", default=True)

# Hoster intelligence (ADR 0027): a hosting record is looked up again after this many days.
HOSTING_REFRESH_DAYS = env_int("HOSTING_REFRESH_DAYS", default=30)
# A customer sees a provider's rounded response history only from this many observed notices on
# (ADR 0027 amendment 2026-09-17); below it, "no response history yet".
HOSTING_HISTORY_MIN_NOTICES = env_int("HOSTING_HISTORY_MIN_NOTICES", default=5)
# Provider directory (ADR 0027 amendment 2026-09-17): a complaint channel a person verified longer
# ago than this is due to be checked again (admin warning, `provider_directory_report`).
PROVIDER_CHANNEL_RECHECK_DAYS = env_int("PROVIDER_CHANNEL_RECHECK_DAYS", default=90)

# Billing (ADR 0032): invoices and SEPA bank transfer. The seller's identity comes from the
# environment only — never from the repository. Issuing refuses while any required value is missing
# (`billing.seller.seller_problems`), so an unconfigured development box simply cannot issue.
BILLING_SELLER_NAME = env_str("BILLING_SELLER_NAME", default="")
# Postal address lines separated by "|", e.g. "Street 1|12345 City|Germany".
BILLING_SELLER_ADDRESS = env_str("BILLING_SELLER_ADDRESS", default="")
BILLING_SELLER_TAX_NUMBER = env_str("BILLING_SELLER_TAX_NUMBER", default="")
BILLING_SELLER_VAT_ID = env_str("BILLING_SELLER_VAT_ID", default="")
BILLING_SELLER_EMAIL = env_str("BILLING_SELLER_EMAIL", default="")
BILLING_SELLER_IBAN = env_str("BILLING_SELLER_IBAN", default="")
BILLING_SELLER_BIC = env_str("BILLING_SELLER_BIC", default="")
BILLING_SELLER_BANK = env_str("BILLING_SELLER_BANK", default="")
# Small-business scheme (§ 19 UStG): "true" or "false", and deliberately no default — charging VAT
# by mistake and forgetting it are both expensive, so issuing refuses until the operator decides.
BILLING_SMALL_BUSINESS = env_str("BILLING_SMALL_BUSINESS", default="")
BILLING_PAYMENT_TERM_DAYS = env_int("BILLING_PAYMENT_TERM_DAYS", default=14)
# Printed before "<year>-<sequence>", e.g. "RE-" gives RE-2026-00001. Change it only at a year
# boundary: numbers must stay unambiguous.
BILLING_INVOICE_NUMBER_PREFIX = env_str("BILLING_INVOICE_NUMBER_PREFIX", default="")

# Rights proof vault (ADR 0034): encrypted contract excerpts, mandates and schedules. Keys only from
# the environment (comma-separated Fernet keys; the first encrypts, all decrypt). The directory must
# be absolute and outside the repository. Without both, proof uploads are refused.
# Issue #92: the pilot is B2B-only and EU-only (operator decision, issue #95 item 7), and a
# declaration alone does not establish business status. `accounts.BusinessStatus` records the
# evidence; this decides what happens when a staff member grants a customer access without it.
# `warn` (the default) shows a message in the admin and lets the save through; `refuse` makes
# `Membership.clean()` reject it. Anything else reads as `warn`: a typo must not refuse every
# membership. The seeding commands are never refused whatever this says.
ONBOARDING_BUSINESS_STATUS_GUARD = env_str("ONBOARDING_BUSINESS_STATUS_GUARD", default="warn")

# Onboarding by invite (issue #115, ADR 0052). Staff issue a single-use link; the creator sets a
# password, signs the versioned Unternehmer declaration, accepts the documents and answers the
# EU-only country question. It is **not** signup: there is still no way for a stranger to create an
# account (ADR 0023). OFF by default — with it off every /api/v1/app/invite route answers the same
# neutral 404 and the staff tooling refuses to issue.
ONBOARDING_INVITES_ENABLED = env_bool("ONBOARDING_INVITES_ENABLED", default=False)
ONBOARDING_INVITE_EXPIRY_DAYS = env_int("ONBOARDING_INVITE_EXPIRY_DAYS", default=14)
# The customer app's origin, so `manage.py invite_customer` can print a link the operator pastes
# into the message he is writing anyway. No mail is sent by any code path (ADR 0052 §2).
ONBOARDING_INVITE_BASE_URL = env_str("ONBOARDING_INVITE_BASE_URL", default="")
# Which documents a redemption presents, as `id:version:sha256:url` entries separated by commas —
# the sha256 of the **published** text the URL resolves to. Ids: `pilot_agreement`,
# `service_privacy_notice`. **Empty is the default and empty refuses**: the pilot agreement and the
# service privacy notice are drafts, so no invite can be completed
# until real documents are published (ADR 0052 §3).
ONBOARDING_DOCUMENTS = env_str("ONBOARDING_DOCUMENTS", default="")
# An invite that is never redeemed (expired,
# revoked, abandoned) would hold its personal data — email, note, declarations, country, a password
# hash until redemption — with no deletion clock. The scheduler's `invite_retention` job deletes
# one `ONBOARDING_INVITE_RETENTION_DAYS` after it stopped being usable (revocation, or expiry).
# A **redeemed** invite is never touched by this: it cascades away with its organisation, never by
# this clock. 30, like CUSTOMER_DATA_RETENTION_DAYS, until the pilot contract stack confirms it.
ONBOARDING_INVITE_RETENTION_DAYS = env_int("ONBOARDING_INVITE_RETENTION_DAYS", default=30)

# Proof of control of the creator's own platform account (ADR 0055, operator decision 2026-09-19):
# we show the creator a one-time code, they place it in the bio of the account whose content we
# protect (or send it from there), and a staff member looks at that public profile **by hand** and
# records what they saw. No identity document and no date of birth is ever stored,
# and no code of ours ever fetches the claimed profile. OFF by default — with it off every
# /api/v1/app/verification route answers 404 and no code can be issued. It has to be set on the
# public host too: the reads travel in the signed bundle and the two writes through the outbox.
ACCOUNT_CONTROL_VERIFICATION_ENABLED = env_bool(
    "ACCOUNT_CONTROL_VERIFICATION_ENABLED", default=False
)

# Address verification and password reset (ADR 0054, issue #149). Both are **mailed by development
# only**: the public host holds no mail transport and gains none, a request there enters the outbox
# and development issues the token, sends the mail and settles it (the same road as a redemption,
# ADR 0052 §9). OFF by default — with it off, or with no transport configured, every route answers
# its neutral response and nothing is sent.
ACCOUNT_EMAIL_FLOWS_ENABLED = env_bool("ACCOUNT_EMAIL_FLOWS_ENABLED", default=False)
#: The envelope sender of both mails. A role address of ours, never a personal one — the same rule
#: as the crawler's User-Agent and the hotline reply address.
ACCOUNT_MAIL_FROM = env_str("ACCOUNT_MAIL_FROM", default="noreply@marceline.studio")
#: Where a reply goes, because `noreply@` must not be a dead end for somebody who did not ask for
#: the mail. Empty leaves the header out.
ACCOUNT_MAIL_REPLY_TO = env_str("ACCOUNT_MAIL_REPLY_TO", default="contact@marceline.studio")
#: How long a reset link works. Short on purpose: it is a credential in somebody's inbox, and the
#: person who asked for it is reading the mail now (operator decision with the brief, 2026-09-19).
ACCOUNT_PASSWORD_RESET_TTL_MINUTES = env_int("ACCOUNT_PASSWORD_RESET_TTL_MINUTES", default=60)
#: How long a verification link works. Longer, because nothing is urgent about it and a creator may
#: open the mail the next day.
ACCOUNT_VERIFICATION_TTL_HOURS = env_int("ACCOUNT_VERIFICATION_TTL_HOURS", default=48)

RIGHTS_VAULT_KEYS = env_str("RIGHTS_VAULT_KEYS", default="")
RIGHTS_VAULT_DIR = env_str("RIGHTS_VAULT_DIR", default="")
RIGHTS_PROOF_MAX_BYTES = env_int("RIGHTS_PROOF_MAX_BYTES", default=10 * 1024 * 1024)

# Cloud fingerprinting (issue #59, ADR 0044): the customer uploads a video, the server hashes it
# and deletes the bytes. OFF by default (preconditions: ADR 0044 amendment of 2026-09-19); with it
# off every /api/v1/app/uploads route answers 404.
CLOUD_FINGERPRINTING_ENABLED = env_bool("CLOUD_FINGERPRINTING_ENABLED", default=False)
# The streaming relay (ADR 0044 §9, amendment 2026-09-19). The **only** way the public host may
# answer an upload route: it holds no spool, writes no chunk anywhere and forwards each read of the
# request body to development's Tailscale-only listener as it reads it. On development this must
# stay false — development is where the bytes land, and a relay pointing at itself is a loop.
CLOUD_UPLOAD_RELAY_MODE = env_bool("CLOUD_UPLOAD_RELAY_MODE", default=False)
# Development's Tailscale-only listener, as production calls it (e.g. http://100.x.y.z:8082), and
# the scoped credential for this one purpose — `manage.py create_service_token --scope upload_relay`
# on development. **Never the export token** (that one is read-only and is not accepted here) and
# never a database role.
CLOUD_RELAY_BASE_URL = env_str("CLOUD_RELAY_BASE_URL", default="")
CLOUD_RELAY_TOKEN = env_str("CLOUD_RELAY_TOKEN", default="")
# Where uploaded bytes wait to be hashed. Absolute, outside the repository, its own Docker volume,
# outside every backup path. Empty = no spool = uploads refused, like the rights vault.
CLOUD_UPLOAD_SPOOL_DIR = env_str("CLOUD_UPLOAD_SPOOL_DIR", default="")
# One chunk of a chunked upload. Small enough that no request ties up a worker for minutes, large
# enough that a 4 GB file is hundreds of requests rather than tens of thousands.
CLOUD_UPLOAD_CHUNK_BYTES = env_int("CLOUD_UPLOAD_CHUNK_BYTES", default=8 * 1024 * 1024)
# The largest file an upload may declare.
CLOUD_UPLOAD_MAX_BYTES = env_int("CLOUD_UPLOAD_MAX_BYTES", default=10 * 1024 * 1024 * 1024)
# The whole spool. A new upload is refused rather than filling the disk Postgres lives on.
CLOUD_UPLOAD_SPOOL_MAX_BYTES = env_int(
    "CLOUD_UPLOAD_SPOOL_MAX_BYTES", default=40 * 1024 * 1024 * 1024
)
# Bytes one organisation may declare per rolling 24 h (the job count is limited separately by
# `marceline/app/upload_throttle.py`).
CLOUD_UPLOAD_DAILY_BYTES_PER_ORG = env_int(
    "CLOUD_UPLOAD_DAILY_BYTES_PER_ORG", default=60 * 1024 * 1024 * 1024
)
# The hard limit on how long bytes may sit in the spool, whatever the database believes. The reaper
# deletes a job directory older than this and marks its row expired; a crashed worker cannot leave
# a file behind for longer.
CLOUD_UPLOAD_TTL_HOURS = env_float("CLOUD_UPLOAD_TTL_HOURS", default=6.0)

_cloud_problems = cloud_upload_problems(
    CloudUploadSettings(
        role=MARCELINE_ROLE,
        public_surface=PUBLIC_SURFACE or None,
        enabled=CLOUD_FINGERPRINTING_ENABLED,
        relay_mode=CLOUD_UPLOAD_RELAY_MODE,
        spool_dir=CLOUD_UPLOAD_SPOOL_DIR,
        relay_base_url=CLOUD_RELAY_BASE_URL,
        relay_token=CLOUD_RELAY_TOKEN,
    )
)
if _cloud_problems:
    raise ImproperlyConfigured(
        "Unsafe cloud fingerprinting settings: " + "; ".join(_cloud_problems)
    )

# Notice drafts (issue #33, ADR 0035): customer-sent drafts only, nothing sent to a real recipient.
# A finding's availability must have been checked this recently before it may go into a draft.
NOTICE_AVAILABILITY_MAX_AGE_HOURS = env_int("NOTICE_AVAILABILITY_MAX_AGE_HOURS", default=48)
# The only addresses Marceline may ever send a sandbox draft's email copy to (comma-separated;
# Marceline-controlled test mailboxes). Empty means the sandbox send path always refuses.
NOTICE_SANDBOX_RECIPIENTS = env_list("NOTICE_SANDBOX_RECIPIENTS", default=[])
NOTICE_SANDBOX_FROM = env_str("NOTICE_SANDBOX_FROM", default="")

# Content-safety media gate (ADR 0018, docs/safety.md). Media is fetched and fingerprinted only
# for pages whose own text scored at least SAFETY_GATE_MIN_SCORE; everything else stays text-only.
# The default mirrors crawler.safety_score.DEFAULT_MIN_SCORE (a test asserts the two agree).
# Lowering this widens what gets fingerprinted — an operator decision, never a code default.
SAFETY_GATE_MIN_SCORE = env_float("SAFETY_GATE_MIN_SCORE", default=0.80)
# How far back `block` incidents on a site count toward its risk factor.
SAFETY_GATE_BLOCK_WINDOW_DAYS = env_int("SAFETY_GATE_BLOCK_WINDOW_DAYS", default=90)

# Targeted indexing (issue #71): the scheduler's `sync_targets` job deactivates pages of no target
# creator and reactivates pages of new targets. OFF by default: the first run changes what matching
# sees, so the operator reviews `deactivate_untargeted --dry-run` and applies once by hand first.
TARGETED_INDEXING_ENABLED = env_bool("TARGETED_INDEXING_ENABLED", default=False)
# Prospect research (docs/prospect-research.md): at most this many seed tasks per prospect are
# queued in any rolling 24 hours, across all sites, so research never crowds out Auftrag targets.
PROSPECT_DAILY_SEED_CAP = env_int("PROSPECT_DAILY_SEED_CAP", default=4)
# Do-not-contact record (issue #98, ADR 0047, docs/do-not-contact.md): the key every recorded "no"
# is hashed with, so the table holds no address, name or handle. Empty = recording and checking are
# both refused; nothing ever hashes with an empty key, which anybody could recompute.
# **Never rotate this key without a migration plan.** The hashes are one-way: a new key orphans
# every entry at once, and the damage is silent — the check goes on answering "allowed" for people
# who said no. Rotating means re-hashing from identifiers we deliberately do not keep, i.e. it
# cannot be done. Keep a copy off this machine, like RIGHTS_VAULT_KEYS.
DO_NOT_CONTACT_HMAC_KEY = env_str("DO_NOT_CONTACT_HMAC_KEY", default="")

# The deletion journal (ADR 0057, DPIA R24, issues #137 / #140 D3). A directory of append-only
# `<segment>.jsonl` files, **outside the database**, holding one line per irreversible privacy
# action: kind, stable key (a row id, a keyed digest, a sha256), timestamp — never the deleted
# content and never a name. A database restore rolls every deletion below back; `manage.py
# reapply_deletions` replays this journal against the restored database instead.
#
# It belongs next to the nightly dumps (`~/backups/marceline/journal`), because
# `infra/backup/backup-run.sh` pushes it off-host under its own restic tag, and because a
# `pg_restore` cannot touch it. Empty = the journal is off, every deletion path logs CRITICAL once,
# and `reapply_deletions` refuses. In a container this is the **container-side** mount point of that
# same host directory (`infra/compose.yaml`), so both write into one directory.
DELETION_JOURNAL_DIR = env_str("DELETION_JOURNAL_DIR", default="")
# Which file in that directory this process appends to. One shared file cannot work: the host-run
# commands write as the operator and the containers as root, and whoever created the file would own
# it. `reapply_deletions` merges every segment by timestamp.
DELETION_JOURNAL_SEGMENT = env_str("DELETION_JOURNAL_SEGMENT", default="deletions")

# Page retention (ADR 0029 amendment 2026-09-17, index.retention): the scheduler's
# `page_retention` job deletes pages that have stayed deactivated (untargeted) longer than
# index.retention.PAGE_RETENTION_MONTHS. OFF by default, exactly like TARGETED_INDEXING_ENABLED
# above: deletion is irreversible, so the operator reviews `page_retention --dry-run` and turns
# this on once by hand.
PAGE_RETENTION_ENABLED = env_bool("PAGE_RETENTION_ENABLED", default=False)

# Safety incident retention (docs/safety.md, issue #100): the scheduler's `safety_retention` job
# closes a reported safety incident the hotline never came back on after 90 days, then deletes an
# incident closed longer than 12 months ago (index.incidents.AUTO_CLOSE_AFTER_DAYS,
# RETAIN_MONTHS_AFTER_CLOSE — code constants, not settings, matching the command's own flags). OFF
# by default for the same reason as PAGE_RETENTION_ENABLED: deletion is irreversible, so the
# operator reads `safety_retention --dry-run` and turns this on once by hand.
SAFETY_RETENTION_ENABLED = env_bool("SAFETY_RETENTION_ENABLED", default=False)

# Customer data deletion (ADR 0045, issue #85, marceline.deletion): the scheduler's
# `customer_data_retention` job deletes what an organisation whose pilot ended, or one original
# marked for removal, left behind, once the window below is up. OFF by default for the same reason
# as PAGE_RETENTION_ENABLED: deletion is irreversible, so the operator reads
# `customer_data_retention --dry-run` and turns this on once by hand. Marking is never guarded by
# it — a mark writes a date and deletes nothing.
CUSTOMER_DATA_DELETION_ENABLED = env_bool("CUSTOMER_DATA_DELETION_ENABLED", default=False)
# Days between the end of a pilot (or the removal of one original) and the deletion. Default 30;
# it is a setting and not a constant so it can follow the contract.
CUSTOMER_DATA_RETENTION_DAYS = env_int("CUSTOMER_DATA_RETENTION_DAYS", default=30)

# The creator's own data requests (ADR 0056, issue #133, marceline.deletion.requests): the two
# buttons on `/app/data` — "send me an export" and "delete my data / end my pilot". OFF by default;
# with it off every /api/v1/app/data-requests route answers one plain 404 and the screen says the
# step is not switched on here. It has to be set on the **public host** too: the read travels in
# the signed bundle and both writes go through the outbox, so a creator who can only reach that
# host is exactly the person this exists for. A request records that they asked and nothing else —
# CUSTOMER_DATA_DELETION_ENABLED above still guards the only code that deletes anything, and the
# date it runs from is still one a human set (ADR 0045).
CUSTOMER_DATA_REQUESTS_ENABLED = env_bool("CUSTOMER_DATA_REQUESTS_ENABLED", default=False)

# Retention on the research side (ADR 0048, issues #103, #104): the scheduler's
# `research_retention` job scrubs discovery query text past its period, forgets prospect research
# nobody acted on and deletes catalogue names nothing uses any more. OFF by default for the same
# reason as PAGE_RETENTION_ENABLED and CUSTOMER_DATA_DELETION_ENABLED: deleting a name is
# irreversible, so the operator reads `research_retention --dry-run` and turns this on once by hand.
RESEARCH_RETENTION_ENABLED = env_bool("RESEARCH_RETENTION_ENABLED", default=False)
# Days a discovery search query's text is kept before it is blanked (the row itself is the spend
# ledger of ADR 0013 and is never deleted). The three periods below are the operator's to confirm;
# they are settings for the same reason CUSTOMER_DATA_RETENTION_DAYS is one.
SEARCH_QUERY_RETENTION_DAYS = env_int("SEARCH_QUERY_RETENTION_DAYS", default=30)
# Days a prospect's research is kept, counted from the day the watchlist entry was added. The
# Art. 14 first-contact draft promises three months.
PROSPECT_RESEARCH_RETENTION_DAYS = env_int("PROSPECT_RESEARCH_RETENTION_DAYS", default=90)
# Days a catalogue name is kept after the pass first saw nothing using it (no target role, no
# customer original, no watchlist entry, no active page, no safety record, no suppression, not in
# sources.md).
CREATOR_NAME_RETENTION_DAYS = env_int("CREATOR_NAME_RETENTION_DAYS", default=90)

# Invite retention (ADR 0052 amendment, accounts.invite_retention): the scheduler's
# `invite_retention` job deletes an unredeemed invite past ONBOARDING_INVITE_RETENTION_DAYS above.
# OFF by default for the same reason as PAGE_RETENTION_ENABLED and RESEARCH_RETENTION_ENABLED:
# deletion is irreversible, so the operator reads `invite_retention --dry-run` and turns this on
# once by hand.
INVITE_RETENTION_ENABLED = env_bool("INVITE_RETENTION_ENABLED", default=False)

# Black-bar removal before hashing page media (docs/matching/black-bars.md).
# OFF by default, and turning it on is an operator decision: it is **not retroactive**. Page pixels
# are discarded after hashing, so only media hashed from then on would be de-barred and the index
# would hold two conventions side by side; the stored hashes of already indexed pages only change
# after a re-fetch. A frame without bars hashes bit-identically either way, so switching it on
# changes nothing for the 99 % of the index that carries no bars, and it needs no new pipeline tag:
# a de-barred page frame is compared with the *existing* reference hashes, which were taken from
# the unpadded original. Reference hashing is deliberately left alone.
MATCHING_CROP_BLACK_BARS = env_bool("MATCHING_CROP_BLACK_BARS", default=False)

# Frame embeddings, the second content signal (ADR 0037, issue: the frame-embedding pilot).
# OFF by default and deliberately so: it costs ~55 ms and 160 MB of resident set per hashing
# process, and the whole point of the pilot is to decide on our own numbers whether that buys
# recall. Turning it on is an operator decision. Nothing is retroactive — page pixels are
# discarded after hashing, so sketches exist only for media fetched after the switch, and the
# matcher must treat a missing sketch as "no information", never as a negative.
MATCHING_EMBEDDINGS_ENABLED = env_bool("MATCHING_EMBEDDINGS_ENABLED", default=False)
# The pinned ONNX image encoder. Empty: `~/.cache/marceline/models/<file>` (see
# `matching.embedding.model_path`). The file is never in the repository or the image.
MATCHING_EMBEDDING_MODEL_PATH = env_str("MATCHING_EMBEDDING_MODEL_PATH", default="")
# Neighbours a page frame's sketch may propose per reference scan. Ten is what the research
# measured recall@10 on; a larger k buys recall the confirmation paths have to pay for.
MATCHING_EMBEDDING_TOP_K = env_int("MATCHING_EMBEDDING_TOP_K", default=10)

# The reference-side crop grid (ADR 0039, docs/matching/reference-crop-grid.md, issue #80).
# OFF by default, and the pre-registered pilot decides adoption, not this default. It switches one
# thing: whether `match_references` puts each grid moment's 16 corner crops into the Hamming index
# and consults the blobs (`matching.crop_index`). It changes nothing that is stored, and turning it
# off again restores today's behaviour exactly — a library without grids behaves identically either
# way, because there is nothing to load.
# Unlike ADR 0037's sketch, a grid hit is ordinary PDQ evidence in PDQ's own units, judged at the
# ordinary threshold. That is also why switching it on is not free: a minimum over 256 correlated
# hashes shifts the whole distance distribution down 10-16 bits (§4.4), so
# **`calibrate_confidence` must be re-run on adoption** or grid-derived matches score generously.
# Cost when on: ~96 MB of matcher resident set at the current library size (measured, see
# `matching/crop_index.py`), 629 MB of blobs, and one full rescan the first time grids appear,
# because the scan fingerprint changes. The page side costs nothing at all — it is retroactive
# over every page hash already stored, with no request and no re-hash.
MATCHING_REFERENCE_CROP_GRID = env_bool("MATCHING_REFERENCE_CROP_GRID", default=False)

# Metadata scoring, the third content signal (ADR 0040, docs/matching/metadata-scoring.md).
# OFF by default, like the second. It costs nothing to run (under a second over the whole index,
# no request, no model, no dependency) and since the ADR 0040 amendment of 2026-09-18 it costs no
# review work either: it writes nothing and asks nobody. What it produces is an *aim* - one more
# source of the browser-capture triage (ADR 0016), "fingerprint this page next", after which PDQ
# decides by itself. So this flag now buys capture visits, which is why it stays an operator
# decision: a capture drives a real browser over a piracy page. It is never evidence - see
# `matching/metadata_candidates.py` for the three guards that keep it that way.
MATCHING_METADATA_CANDIDATES_ENABLED = env_bool(
    "MATCHING_METADATA_CANDIDATES_ENABLED", default=False
)

# Browser frame capture (ADR 0016; the operator enabled the capability on 2026-09-16).
# OFF by default and deliberately so: one capture drives a real browser over a piracy page, which
# is the most expensive and most sensitive thing this system does. Turning it on is an
# operator decision per deployment, never a code default. Triage (`triage_browser_capture`) runs
# either way; only `capture_worker` looks at this flag.
BROWSER_CAPTURE_ENABLED = env_bool("BROWSER_CAPTURE_ENABLED", default=False)
# Visits, not requests: one visit is 50-300 HTTP requests the browser makes on its own.
BROWSER_CAPTURE_PER_SITE_PER_DAY = env_int("BROWSER_CAPTURE_PER_SITE_PER_DAY", default=5)
BROWSER_CAPTURE_TOTAL_PER_DAY = env_int("BROWSER_CAPTURE_TOTAL_PER_DAY", default=20)
# Daily window in UTC; may wrap midnight. Nothing is captured outside it.
BROWSER_CAPTURE_WINDOW_UTC = env_str("BROWSER_CAPTURE_WINDOW_UTC", default="01:00-05:00")
# Browsers at once. One, and a Postgres advisory lock enforces one worker process on top.
BROWSER_CAPTURE_MAX_CONCURRENCY = env_int("BROWSER_CAPTURE_MAX_CONCURRENCY", default=1)
# Per-capture caps (marceline/capture/plan.py): frames sampled, seconds spent sampling, bytes the
# browser may pull, and the watchdog for the whole visit.
BROWSER_CAPTURE_MAX_FRAMES = env_int("BROWSER_CAPTURE_MAX_FRAMES", default=24)
BROWSER_CAPTURE_MAX_SECONDS = env_float("BROWSER_CAPTURE_MAX_SECONDS", default=120.0)
BROWSER_CAPTURE_MAX_BYTES = env_int("BROWSER_CAPTURE_MAX_BYTES", default=200 * 1024 * 1024)
BROWSER_CAPTURE_TIMEOUT_S = env_float("BROWSER_CAPTURE_TIMEOUT_S", default=300.0)
# Where Playwright is installed. Empty = the parity harness install, frontend/parity/node_modules
# (frontend/parity/README.md). The backend itself has no browser dependency.
BROWSER_CAPTURE_NODE_MODULES = env_str("BROWSER_CAPTURE_NODE_MODULES", default="")
# Browser engine (docs/research/browser-capture-no-video.md): `chromium` (Playwright's headless
# shell: plays H.264/AAC, closest frames, least memory), `firefox` (Playwright's build) or `chrome`
# (Google Chrome: the `chrome` channel, or BROWSER_CAPTURE_EXECUTABLE).
BROWSER_CAPTURE_ENGINE = env_str("BROWSER_CAPTURE_ENGINE", default="chromium")
# Optional browser binary for the engine, e.g. an unpacked Google Chrome. Empty = Playwright's.
BROWSER_CAPTURE_EXECUTABLE = env_str("BROWSER_CAPTURE_EXECUTABLE", default="")

# Optional source discovery (paid searches are guarded in crawler.discovery).
DATAFORSEO_LOGIN = env_str("DATAFORSEO_LOGIN", default="")
DATAFORSEO_PASSWORD = env_str("DATAFORSEO_PASSWORD", default="")
DISCOVERY_LOCATION_CODE = env_int("DISCOVERY_LOCATION_CODE", default=2840)
DISCOVERY_LANGUAGE = env_str("DISCOVERY_LANGUAGE", default="en")
DISCOVERY_MAX_CALLS_PER_RUN = env_int("DISCOVERY_MAX_CALLS_PER_RUN", default=60)
DISCOVERY_DAILY_BUDGET_USD = env_str("DISCOVERY_DAILY_BUDGET_USD", default="0.25")
# Issue #20: the scheduler's own periodic `discover_sources` run, proposing candidate domains for
# human review only — it never allowlists or enables a site (ADR 0013). OFF by default, exactly
# like TARGETED_INDEXING_ENABLED above: discovery is a paid search API, and the operator turns
# this on once by hand. When on, the run goes through the same spend guard as every manual
# `discover_sources` invocation (DISCOVERY_MAX_CALLS_PER_RUN per run, DISCOVERY_DAILY_BUDGET_USD
# as a rolling 24h total shared with every other caller) and the same query-safety refusal
# (safety terms, minor near misses, German non-consent wording) — nothing new is invented here.
DISCOVERY_SCHEDULE_ENABLED = env_bool("DISCOVERY_SCHEDULE_ENABLED", default=False)

# LLM extraction as repair when an adapter breaks (issue #22, ADR 0042, crawler/repair.py).
# **This costs money per call**, so it is off by default and the operator turns it on knowingly;
# with it off nothing in the crawler ever contacts a model. Provider is OpenRouter (pay-per-use),
# reached over plain HTTPS with no vendor SDK.
CRAWLER_LLM_REPAIR_ENABLED = env_bool("CRAWLER_LLM_REPAIR_ENABLED", default=False)
OPENROUTER_API_KEY = env_str("OPENROUTER_API_KEY", default="")
# Whatever the operator has chosen on OpenRouter; empty is the same as disabled. No default model
# is pinned here on purpose: picking one would be picking a price.
CRAWLER_LLM_MODEL = env_str("CRAWLER_LLM_MODEL", default="")
# The hard cost cap: repair calls per site per rolling 24 h, counted from the `crawler.LLMCall`
# rows themselves, so a restart cannot reset it. A broken adapter is fixed by hand within a day;
# this is enough to keep the worst of a site's pages usable until then, not to run a crawl on.
CRAWLER_LLM_REPAIR_DAILY_CALLS_PER_SITE = env_int(
    "CRAWLER_LLM_REPAIR_DAILY_CALLS_PER_SITE", default=50
)

# Active safety notification (issue #40, docs/safety.md, crawler/alerts.py): one short mail to the
# operator when a page is newly blocked **by a rule that opens a safety incident** — every other
# block is counted in the daily digest below instead (2026-09-18). Off by default — both must be
# set, and neither is ever a code default or committed value; the operator supplies them once a
# mailbox exists. The same pair switches the digest on.
SAFETY_ALERT_TO = env_str("SAFETY_ALERT_TO", default="")
SAFETY_ALERT_FROM = env_str("SAFETY_ALERT_FROM", default="")
# Pages blocked within this many minutes of each other are sent as one mail (crawler/alerts.py).
SAFETY_ALERT_INTERVAL_MINUTES = env_int("SAFETY_ALERT_INTERVAL_MINUTES", default=10)
# Daily safety digest (crawler/safety_digest.py): the UTC hour the scheduler sends it at, once a
# day, so it is in the mailbox before the working day starts. A window with no block sends nothing,
# but the scheduler job still records itself in `core.JobRun` (`make status`).
SAFETY_DIGEST_HOUR_UTC = env_int("SAFETY_DIGEST_HOUR_UTC", default=6)
# Origin the moderation admin is reached at (e.g. the Tailscale address), prepended to the admin
# change-page link in the alert. Empty keeps the link relative (still correct, just not a full URL).
SAFETY_ALERT_MODERATION_URL = env_str("SAFETY_ALERT_MODERATION_URL", default="")

# Adapter breakage alert (issue #22, ADR 0042, crawler/adapter_alerts.py): one mail when a site's
# parser is newly recorded broken. Empty falls back to the safety pair above — one operator, one
# mailbox — and with neither pair set nothing is sent, which is the default. Read here rather than
# straight from the environment (as the backup alert does) because `infra/compose.yaml` passes
# these into the crawler containers, and every variable compose passes must have a reader in
# settings.py (`marceline/tests/test_project_status.py`).
ADAPTER_ALERT_TO = env_str("ADAPTER_ALERT_TO", default="")
ADAPTER_ALERT_FROM = env_str("ADAPTER_ALERT_FROM", default="")

# Operator mail consolidation (2026-09-19, core/operator_mail.py, crawler/safety_digest.py): an
# incident-rule block, a new/changed TDM/AI reservation and a broken adapter used to each mail the
# operator the moment they happened, on top of the daily digest. All three now fold into the one
# daily report by default. A comma list of `core.operator_mail.KINDS` brings one back to mailing on
# its own without a code change; empty (the default) means none of them do. Never a code default
# other than empty, and never committed with a real value.
OPERATOR_MAIL_IMMEDIATE = env_str("OPERATOR_MAIL_IMMEDIATE", default="")

# Catalogue share (issue #107, ADR 0049, catalog/catalogue_size.py): the percentage of a site's own
# catalogue we may hold, ever indexed, before the operator is mailed once — through the safety
# address pair above, so no new mailbox. **Monitoring only**: crossing it pauses nothing, disables
# nothing and slows nothing down.
#
# Why 25. It is above the shares measured so far by a clear margin, so it does not fire on today's
# practice and a crossing means something changed; and it is far below "most of the catalogue".
# The figure is a placeholder. 0 switches the alert off.
CATALOGUE_SHARE_ALERT_PERCENT = env_float("CATALOGUE_SHARE_ALERT_PERCENT", default=25.0)

# Reply address printed in the hotline report draft (issue #66, index/incident_report.py): the
# project's reporting role mailbox (docs/safety.md). A role address, never a personal one; empty leaves the line out.
SAFETY_REPORT_REPLY_TO = env_str("SAFETY_REPORT_REPLY_TO", default="reporting@marceline.studio")

# Outgoing mail (currently used only by the safety alert above), as Django 6.1's `MAILERS`
# (the classic single EMAIL_* settings are deprecated in this version — mixing the two raises
# ImproperlyConfigured, so nothing here uses them). The environment variable names still follow
# the familiar EMAIL_* convention operators know from any Django app / FastMail's own SMTP docs.
# Dev default is the console backend, which never opens a network connection; an option is only
# added when its variable is actually set, so the console/locmem backends never see an SMTP-only
# kwarg they would reject. Set EMAIL_BACKEND to the SMTP backend plus EMAIL_HOST etc. once a
# mailbox exists (e.g. smtp.example.com:587, TLS). Never a hardcoded or committed
# credential — .env only (.env.example).
_MAIL_OPTIONS: dict[str, object] = {}
if env_is_set("EMAIL_HOST"):
    _MAIL_OPTIONS["host"] = env_str("EMAIL_HOST", default="")
if env_is_set("EMAIL_PORT"):
    _MAIL_OPTIONS["port"] = env_int("EMAIL_PORT", default=587)
if env_is_set("EMAIL_HOST_USER"):
    _MAIL_OPTIONS["username"] = env_str("EMAIL_HOST_USER", default="")
if env_is_set("EMAIL_HOST_PASSWORD"):
    _MAIL_OPTIONS["password"] = env_str("EMAIL_HOST_PASSWORD", default="")
if env_is_set("EMAIL_USE_TLS"):
    _MAIL_OPTIONS["use_tls"] = env_bool("EMAIL_USE_TLS", default=True)
if env_is_set("EMAIL_USE_SSL"):
    _MAIL_OPTIONS["use_ssl"] = env_bool("EMAIL_USE_SSL", default=False)
# Production defaults to SMTP: a mail that cannot be delivered should fail loudly, not be printed
# to a log nobody reads (and Django's deploy check refuses the console backend).
_MAIL_BACKEND = env_str(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend"
    if IS_PROD
    else "django.core.mail.backends.console.EmailBackend",
)
# Every network backend gets a timeout, 10 s unless EMAIL_TIMEOUT says otherwise: Django's SMTP
# default is none, and a mail server that never answers would hang the crawler or media worker
# sending a safety alert. Django's local backends open no connection and refuse the option, so
# compose's EMAIL_TIMEOUT default must not reach them.
_LOCAL_MAIL_BACKENDS = {
    f"django.core.mail.backends.{name}.EmailBackend"
    for name in ("console", "locmem", "filebased", "dummy")
}
if _MAIL_BACKEND not in _LOCAL_MAIL_BACKENDS:
    _MAIL_OPTIONS["timeout"] = env_int("EMAIL_TIMEOUT", default=10)

MAILERS = {"default": {"BACKEND": _MAIL_BACKEND, "OPTIONS": _MAIL_OPTIONS}}

# Error reporting (issue #37, core/sentry.py): off unless SENTRY_DSN is set, and then it refuses to
# start without the SDK rather than report nothing. No PII, no request bodies, no tracing.
SENTRY_DSN = env_str("SENTRY_DSN", default="")
init_sentry(
    dsn=SENTRY_DSN,
    environment=env_str("SENTRY_ENVIRONMENT", default=DJANGO_ENV),
    release=env_str("MARCELINE_RELEASE", default=""),
)
