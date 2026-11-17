"""Optional error reporting to Sentry (issue #37, ADR 0028). Off unless `SENTRY_DSN` is set.

The operator chose Sentry's **EU data region** (2026-09-17): a DSN that does not point at the EU
ingest host (`*.de.sentry.io`) refuses to start, so a copy-pasted US project cannot quietly send
reports to the wrong jurisdiction.

`sentry-sdk` is a dependency (operator approval 2026-09-17) but is imported by name only once a DSN
is set, so this module costs nothing while the DSN is empty; an image built without it refuses to
start instead of silently reporting nothing.

What leaves the machine is kept to what a stack trace needs:
- no default PII (user ids, client addresses), no local variables in frames (a login view holds a
  password in one), no request bodies, no tracing;
- `scrub_event` removes, before sending, request data, cookies, headers, query strings, the user
  and environment blocks, and breadcrumb payloads (SQL parameters can carry usernames).

**`logger.exception` needs its own scrubbing here, not just at the call site** (operator decision
2026-09-18). `core.log_scrub.scrub_urls` cleans the *message* a caller builds; a `logger.exception`
call also hands the SDK the live exception object, and `DjangoIntegration`'s logging integration
renders the exception's own `str()` into `event["exception"]["values"][i]["value"]` independently
of the log message — a message-level scrub run at the call site never touches that field, since the
SDK reads it straight from the exception, not from the formatted log record. `scrub_event` is the
one place that sees the finished event, so it scrubs that field (and a breadcrumb's `message`,
which keeps the log line's text even after its `data` is dropped) the same way.
"""

import importlib
from typing import Any
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from core.log_scrub import scrub_urls

EU_INGEST_SUFFIX = ".de.sentry.io"
# What of the request an error report keeps: enough to find the endpoint, nothing a user sent.
KEPT_REQUEST_KEYS = frozenset({"method", "url"})


def is_eu_dsn(dsn: str) -> bool:
    host = urlsplit(dsn).hostname or ""
    return host.endswith(EU_INGEST_SUFFIX)


def scrub_event(event: dict[str, Any], hint: object = None) -> dict[str, Any]:
    """`before_send`: strip everything a customer or a request could have put into the event."""
    request = event.get("request")
    if isinstance(request, dict):
        kept = {key: value for key, value in request.items() if key in KEPT_REQUEST_KEYS}
        if isinstance(kept.get("url"), str):
            kept["url"] = kept["url"].split("?", 1)[0]
        event["request"] = kept
    event.pop("user", None)
    for crumb in (event.get("breadcrumbs") or {}).get("values", []) or []:
        if isinstance(crumb, dict):
            crumb.pop("data", None)
            if isinstance(crumb.get("message"), str):
                crumb["message"] = scrub_urls(crumb["message"])
    for value in (event.get("exception") or {}).get("values", []) or []:
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            value["value"] = scrub_urls(value["value"])
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for key in ("message", "formatted"):
            if isinstance(logentry.get(key), str):
                logentry[key] = scrub_urls(logentry[key])
    return event


def sentry_options(*, dsn: str, environment: str, release: str) -> dict[str, Any]:
    """Keyword arguments for `sentry_sdk.init`, without the integrations."""
    return {
        "dsn": dsn,
        "environment": environment,
        "release": release or None,
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "traces_sample_rate": 0.0,
        "before_send": scrub_event,
    }


def init_sentry(*, dsn: str, environment: str, release: str = "") -> bool:
    """Initialise Sentry when a DSN is configured. Returns whether reporting is on."""
    if not dsn:
        return False
    if not is_eu_dsn(dsn):
        raise ImproperlyConfigured(
            "SENTRY_DSN must point at Sentry's EU data region (an ingest host ending in "
            f"{EU_INGEST_SUFFIX}); create the project in an EU organisation"
        )
    try:
        sdk = importlib.import_module("sentry_sdk")
        django_integration = importlib.import_module("sentry_sdk.integrations.django")
    except ImportError as exc:
        raise ImproperlyConfigured(
            "SENTRY_DSN is set but sentry-sdk is not installed; add it with `uv add sentry-sdk` "
            "or leave SENTRY_DSN empty"
        ) from exc
    sdk.init(
        integrations=[django_integration.DjangoIntegration()],
        **sentry_options(dsn=dsn, environment=environment, release=release),
    )
    return True
