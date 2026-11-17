"""Strip URLs out of text before it reaches a log message (operator decision 2026-09-18).

A third-party exception — an `httpx` transport error, `core.robots`'s `RobotsUnavailableError` —
can quote the URL it was raised about (`robots.txt for https://example.com: ...`). Once a Sentry
DSN is set, `core.sentry` ships WARNING/ERROR log records to Sentry (and INFO ones ride along as
breadcrumbs on a later event), so a URL a caller never chose to log would leave our systems that
way. `scrub_urls` keeps the rest of the text — the row id and site domain a caller adds itself are
untouched — and replaces only the URL with a stable `<url>` marker, so the record stays
reconstructable from what the caller already logs plus the database (`CrawlTask.last_error`,
`Media.last_error` keep the original text; nothing here touches them).

A bare domain with no path (`site01.example`) is deliberately left alone: the site domain is already
the identifier this module exists to keep in the message (the pattern from commit 9ba5d823, "log
ids instead of URLs"), so redacting it would only hide information we mean to keep. What must go
is the *path* — the part that names a specific page or file.

Lives in `core`, which may depend on nothing else, so every app can use it.
"""

import re

#: What a stripped URL is replaced with.
MARKER = "<url>"

# A URL with an explicit scheme, or protocol-relative (`//host/path`) — matched up to the next
# whitespace or quote, the shape httpx and core.robots write them in
# (`robots.txt for https://example.com: ...`, a redirect to `//cdn.example.com/x.jpg`). Redacted
# whether or not it carries a path: the scheme alone marks it unambiguously as a URL.
_SCHEMED = r"(?:https?:)?//[^\s'\"<>]+"

# A bare host with no scheme at all, but only together with a path — an IPv4 address or a
# dotted hostname with an alphabetic-looking TLD, immediately followed by "/something". A domain
# on its own (no slash) is not matched; see the module docstring.
_BARE_HOST_PATH = (
    r"\b(?:"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,24}"  # example.com
    r"|(?:\d{1,3}\.){3}\d{1,3}"  # 203.0.113.9
    r")/[^\s'\"<>]*"
)

_URL_RE = re.compile(f"(?:{_SCHEMED})|(?:{_BARE_HOST_PATH})")

# Trailing characters that are almost always the surrounding sentence's punctuation, not part of
# the URL itself (`... for https://example.com/x: connection refused` — the colon is ours).
_TRAILING_PUNCT = ".,;:!?)]}'\""


def scrub_urls(text: str) -> str:
    """Replace every URL-shaped run in `text` with `<url>`; everything else is left untouched."""

    def _redact(match: re.Match[str]) -> str:
        found = match.group()
        trail = ""
        while found and found[-1] in _TRAILING_PUNCT:
            trail = found[-1] + trail
            found = found[:-1]
        return f"{MARKER}{trail}" if found else match.group()

    return _URL_RE.sub(_redact, text)


def scrub_exception(exc: BaseException) -> str:
    """`TypeName: message` with any URL in `message` replaced by `<url>`.

    For a call site that logs `f"{type(exc).__name__}: {exc}"` (the idiom used across the crawler
    and media worker for a stored, unscrubbed field), use this instead for the copy that goes to
    the logger.
    """
    return f"{type(exc).__name__}: {scrub_urls(str(exc))}"
