"""`scrub_urls` / `scrub_exception`: what they strip and what they leave alone."""

import pytest

from core.log_scrub import MARKER, scrub_exception, scrub_urls


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # http:// / https://
        (
            "robots.txt for https://site01.example: connection refused",
            f"robots.txt for {MARKER}: connection refused",
        ),
        (
            "fetch failed for http://cdn.example.test/a/b.mp4",
            f"fetch failed for {MARKER}",
        ),
        # scheme-relative //host/path
        (
            "poster at //cdn.example.test/img/1.jpg did not decode",
            f"poster at {MARKER} did not decode",
        ),
        # bare host/path (no scheme)
        (
            "redirected to site01.example/watch?id=5 before the check ran",
            f"redirected to {MARKER} before the check ran",
        ),
        # bare host/path with an IPv4 host
        (
            "connect to 203.0.113.9/status failed",
            f"connect to {MARKER} failed",
        ),
        # several URLs in one message
        (
            "https://a.example/x and https://b.example/y both failed",
            f"{MARKER} and {MARKER} both failed",
        ),
    ],
)
def test_scrub_urls_replaces_the_url_and_keeps_the_rest(text: str, expected: str) -> None:
    assert scrub_urls(text) == expected


def test_a_bare_domain_with_no_path_is_left_alone() -> None:
    """The site domain is the identifier this module exists to keep (commit 9ba5d823)."""
    text = "site01.example paused until 2026-09-18T12:00:00: 3 failure(s) in a row"

    assert scrub_urls(text) == text


def test_the_word_robots_txt_is_not_mistaken_for_a_bare_host_path() -> None:
    """No trailing slash after `robots.txt`, so the bare host/path pattern must not touch it."""
    text = "robots.txt unavailable, retrying"

    assert scrub_urls(text) == text


def test_trailing_sentence_punctuation_stays_outside_the_marker() -> None:
    assert scrub_urls("see https://example.test/x.") == f"see {MARKER}."
    assert scrub_urls("(https://example.test/x)") == f"({MARKER})"
    assert scrub_urls("'https://example.test/x'") == f"'{MARKER}'"


def test_text_with_no_url_is_unchanged() -> None:
    text = "hash child died: EOFError"

    assert scrub_urls(text) == text


def test_scrub_exception_keeps_the_type_name_and_scrubs_the_message() -> None:
    exc = ValueError("robots.txt for https://site01.example: HTTP 503")

    assert scrub_exception(exc) == f"ValueError: robots.txt for {MARKER}: HTTP 503"


def test_scrub_exception_on_a_message_with_no_url() -> None:
    exc = RuntimeError("hash child died: EOFError")

    assert scrub_exception(exc) == "RuntimeError: hash child died: EOFError"
