import sys
from types import ModuleType
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

from core.sentry import init_sentry, scrub_event

EU_DSN = "https://key@o123.ingest.de.sentry.io/456"
US_DSN = "https://key@o123.ingest.us.sentry.io/456"


def test_no_dsn_means_no_import_and_no_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # importing it would raise

    assert init_sentry(dsn="", environment="prod") is False


@pytest.mark.parametrize(
    "dsn",
    [US_DSN, "https://key@sentry.io/1", "https://key@o1.ingest.de.sentry.io.evil.test/1"],
)
def test_a_dsn_outside_the_eu_region_refuses_to_start(dsn: str) -> None:
    with pytest.raises(ImproperlyConfigured, match="EU data region"):
        init_sentry(dsn=dsn, environment="prod")


def test_dsn_without_the_sdk_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)

    with pytest.raises(ImproperlyConfigured, match="sentry-sdk is not installed"):
        init_sentry(dsn=EU_DSN, environment="prod")


def test_dsn_initialises_without_pii_locals_bodies_or_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    sdk: Any = ModuleType("sentry_sdk")
    sdk.init = lambda **kwargs: calls.append(kwargs)
    django_integration: Any = ModuleType("sentry_sdk.integrations.django")
    django_integration.DjangoIntegration = lambda: "django"
    monkeypatch.setitem(sys.modules, "sentry_sdk", sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", ModuleType("integrations"))
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.django", django_integration)

    assert init_sentry(dsn=EU_DSN, environment="prod", release="abc123") is True

    (options,) = calls
    assert options["integrations"] == ["django"]
    assert options["dsn"] == EU_DSN
    assert options["release"] == "abc123"
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["traces_sample_rate"] == 0.0
    assert options["before_send"] is scrub_event


def test_scrub_keeps_the_endpoint_and_drops_everything_a_user_sent() -> None:
    event: dict[str, Any] = {
        "request": {
            "method": "POST",
            "url": "https://app.test/api/v1/app/auth/login?next=/app",
            "data": {"username": "someone", "password": "hunter2"},
            "cookies": {"sessionid": "secret"},
            "headers": {"Cookie": "sessionid=secret", "X-CSRFToken": "token"},
            "query_string": "next=/app",
            "env": {"REMOTE_ADDR": "203.0.113.9"},
        },
        "user": {"id": 7, "ip_address": "203.0.113.9"},
        "breadcrumbs": {"values": [{"category": "query", "data": {"params": ["someone"]}}]},
        "exception": {"values": [{"type": "ValueError"}]},
    }

    scrubbed = scrub_event(event)

    assert scrubbed["request"] == {
        "method": "POST",
        "url": "https://app.test/api/v1/app/auth/login",
    }
    assert "user" not in scrubbed
    assert scrubbed["breadcrumbs"]["values"] == [{"category": "query"}]
    assert scrubbed["exception"] == {"values": [{"type": "ValueError"}]}
    for secret in ("hunter2", "someone", "secret", "token", "203.0.113.9"):
        assert secret not in repr(scrubbed)


def test_scrub_strips_a_url_from_a_captured_exceptions_own_message() -> None:
    """`logger.exception` hands the SDK the exception object itself: the integration renders its
    `str()` straight into `exception.values[].value`, bypassing any scrub applied to the log
    message at the call site (core.log_scrub). This is the one place left to catch it."""
    event: dict[str, Any] = {
        "exception": {
            "values": [
                {
                    "type": "RobotsUnavailableError",
                    "value": "robots.txt for https://site01.example/secret/path: HTTP 503",
                }
            ]
        }
    }

    scrubbed = scrub_event(event)

    value = scrubbed["exception"]["values"][0]["value"]
    assert "site01.example/secret/path" not in value
    assert value == "robots.txt for <url>: HTTP 503"


def test_scrub_strips_a_url_from_a_breadcrumbs_message_too() -> None:
    """`data` is dropped already; the crumb's own `message` (the formatted log line) is not."""
    event: dict[str, Any] = {
        "breadcrumbs": {
            "values": [
                {"category": "logger", "message": "fetch failed for https://site01.example/x/y.mp4"}
            ]
        }
    }

    scrubbed = scrub_event(event)

    assert "site01.example/x/y.mp4" not in repr(scrubbed)
    assert scrubbed["breadcrumbs"]["values"][0]["message"] == "fetch failed for <url>"


def test_the_installed_sdk_accepts_these_options() -> None:
    """`sentry-sdk` is a dependency now: the real client takes every option, and sends nothing."""
    sdk = pytest.importorskip("sentry_sdk")
    from sentry_sdk.integrations.django import DjangoIntegration

    from core.sentry import sentry_options

    client = sdk.Client(
        integrations=[DjangoIntegration()],
        **sentry_options(dsn=EU_DSN, environment="test", release=""),
    )
    try:
        assert client.options["send_default_pii"] is False
        assert client.options["include_local_variables"] is False
        assert client.options["traces_sample_rate"] == 0.0
    finally:
        client.close(timeout=0)
