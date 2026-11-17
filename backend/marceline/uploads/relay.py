"""The public host's streaming relay for cloud fingerprinting (ADR 0044 §9, amendment 2026-09-19).

The one way `dash.marceline.studio` may answer an upload route. Production **holds no byte of a
customer's file**: it reads the request body in `READ_BYTES` pieces and hands each piece straight to
development's Tailscale-only listener as it reads it, then answers with what development answered.

What that means concretely, and what the tests in `marceline/tests/test_upload_relay.py` prove:

- **Nothing is written.** No spool (the settings refuse one here,
  `core/deploy.cloud_upload_problems`), no model row (this module imports no model and issues no
  query), no outbox item (a relayed request is not a customer write in ADR 0028's sense — it is
  answered synchronously by the machine that owns the data), no temporary file, and nothing in a log
  or a Sentry event beyond a code. `HttpRequest.read` streams from `wsgi.input`; the body is never
  handed to `request.body`, `request.POST` or `request.FILES`, so Django's multipart handlers — the
  only thing in Django that writes an upload to `FILE_UPLOAD_TEMP_DIR` — are never engaged.
- **At most `READ_BYTES` in memory**, whatever the chunk size is, because the outgoing body is a
  generator over those reads rather than a `bytes`.
- **Its own credential.** A `ServiceToken` of scope `upload_relay`, presented as a bearer token to
  `/internal/v1/relay/…` and accepted nowhere else. Never the read-only export token (development
  checks the scope), never a database role, never the signing key.
- **The customer's file name is still never transmitted**: it never reaches production either. The
  browser pre-fills the title with it and only the title travels (ADR 0044 § 3).

Who decides what, and the accepted risk. Production authenticates the session and checks CSRF, then
*attests* the acting organisation and user in two headers. Development trusts that attestation — so
the relay credential can act for any customer of ours, which is a real widening of ADR 0028's
read-only export token and was accepted as such by the operator on 2026-09-18 (issue #59). It is
bounded by three things: the listener is reachable over Tailscale only, the token's scope reaches
nothing but these six routes, and the credential may only append to, complete or cancel an upload it
created itself (`MediaUpload.origin`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Final

import httpx
from django.conf import settings
from django.http import HttpRequest, HttpResponse

logger = logging.getLogger("marceline.uploads.relay")

#: Development's side of the relay (`marceline/internal_api.py`), under the prefix
#: `public_surface.RELAY_PREFIX` — development-only and on the `export` surface.
RELAY_PREFIX: Final = "/internal/v1/relay"
#: How much of a chunk is held at once. Deliberately far below the chunk size: a customer's bytes
#: are in this process's memory for as long as one socket read, and never longer.
READ_BYTES: Final = 64 * 1024
#: Production attests who is acting; development resolves these to its own rows.
ORGANISATION_HEADER: Final = "X-Marceline-Relay-Organisation"
USER_HEADER: Final = "X-Marceline-Relay-User"
#: Timeouts, per socket operation (httpx applies connect/read/write/pool separately) and not a
#: deadline for the whole relayed request: the chunk's body is pulled from the customer's own
#: connection, so a slow uplink makes the request long without making any single write slow. A
#: chunk is one 8 MiB body over Tailscale; a status read is a few hundred bytes.
JSON_TIMEOUT_S: Final = 15.0
CHUNK_TIMEOUT_S: Final = 300.0
#: The answers this relay hands to the customer unchanged — exactly the statuses the upload API
#: itself documents. Anything else (a bad credential, a role mismatch, a proxy's own page) becomes
#: one 503 with a code, so a relay problem can never read to the browser as "you are signed out".
PASS_THROUGH_STATUSES: Final = frozenset({200, 201, 400, 403, 404, 409, 413, 429, 503})
#: A status answer is a few hundred bytes; a list of 50 a few thousand. Nothing here is large, so an
#: answer above this is refused rather than truncated — half a JSON document is worse than a code.
MAX_ANSWER_BYTES: Final = 256 * 1024
#: Content types Django's own middleware would parse out from under us (see `forward_chunk`). A
#: chunk that claims to be a form is refused rather than relayed empty.
FORM_CONTENT_TYPES: Final = frozenset({"multipart/form-data", "application/x-www-form-urlencoded"})

#: Test hook, like `delivery/client.py`: an `httpx.MockTransport` here replaces the network.
transport: httpx.BaseTransport | None = None


def configured() -> bool:
    """Whether this process is set up to relay: the mode, the address and the credential."""
    return bool(
        settings.CLOUD_UPLOAD_RELAY_MODE
        and settings.CLOUD_RELAY_BASE_URL
        and settings.CLOUD_RELAY_TOKEN
    )


def active() -> bool:
    """Whether an upload route on this machine must be relayed rather than served."""
    return bool(settings.CLOUD_FINGERPRINTING_ENABLED) and configured()


def _unavailable() -> HttpResponse:
    """The one answer for every relay failure: a code, never a reason built from the other side."""
    return HttpResponse(
        b'{"detail": "uploads_unavailable", "message": ""}',
        status=503,
        content_type="application/json",
    )


def _headers(
    *,
    organisation_id: int,
    user_id: int,
    content_type: str | None = None,
    content_length: int | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.CLOUD_RELAY_TOKEN}",
        ORGANISATION_HEADER: str(int(organisation_id)),
        USER_HEADER: str(int(user_id)),
    }
    if content_type:
        headers["Content-Type"] = content_type
    if content_length is not None:
        # Explicit, so httpx sends a plain body with a length rather than `Transfer-Encoding:
        # chunked` (which it uses for an iterator with no length) — development then reads the body
        # exactly as it reads a browser's.
        headers["Content-Length"] = str(int(content_length))
    return headers


def _pieces(request: HttpRequest, length: int) -> Iterator[bytes]:
    """The request body, in `READ_BYTES` pieces, read from the socket as they are sent.

    Never `request.body`: that would materialise the whole chunk and, past
    `DATA_UPLOAD_MAX_MEMORY_SIZE`, raise instead. A short read is a truncated upload; development's
    own digest check refuses it, and the piece count is not something this process reasons about.
    """
    remaining = length
    while remaining > 0:
        piece = request.read(min(READ_BYTES, remaining))
        if not piece:
            return
        remaining -= len(piece)
        yield piece


def _answer(response: httpx.Response) -> HttpResponse:
    if response.status_code not in PASS_THROUGH_STATUSES:
        logger.warning("upload relay: development answered %s", response.status_code)
        return _unavailable()
    body = response.content
    if len(body) > MAX_ANSWER_BYTES:
        logger.warning("upload relay: development's answer was %s bytes", len(body))
        return _unavailable()
    answer = HttpResponse(
        body,
        status=response.status_code,
        content_type=response.headers.get("Content-Type", "application/json"),
    )
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        answer["Retry-After"] = retry_after
    return answer


def _request(
    method: str,
    path: str,
    *,
    organisation_id: int,
    user_id: int,
    content: bytes | Iterator[bytes] | None,
    content_type: str | None,
    content_length: int | None,
    timeout: float,
) -> HttpResponse:
    url = str(settings.CLOUD_RELAY_BASE_URL).rstrip("/") + RELAY_PREFIX + path
    headers = _headers(
        organisation_id=organisation_id,
        user_id=user_id,
        content_type=content_type,
        content_length=content_length,
    )
    try:
        with httpx.Client(timeout=timeout, transport=transport, follow_redirects=False) as client:
            response = client.request(method, url, headers=headers, content=content)
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as error:
        # Three families, one answer. `httpx.HTTPError` is every transport failure; `InvalidURL` is
        # a misconfigured `CLOUD_RELAY_BASE_URL`, which nothing else would catch; and `OSError` is
        # the customer's own connection dying while `_pieces` reads it (Django raises
        # `UnreadablePostError`, an OSError) — a disconnect is not a server error and must not
        # become a 500 and a Sentry event. The class name, never the message: an httpx message
        # carries the URL, and a read error's carries whatever Django knows about the socket.
        logger.warning("upload relay: %s", type(error).__name__)
        return _unavailable()
    return _answer(response)


def forward_json(
    request: HttpRequest, path: str, *, method: str, organisation_id: int, user_id: int
) -> HttpResponse:
    """Relay one small JSON request (declare, complete) or read (status, list, cancel).

    A read is relayed for the same reason a write is: the row lives on development and is deleted
    within minutes of being created, so there is nothing for a signed bundle to carry and no version
    of "eventually consistent" that a progress bar could survive.
    """
    body = request.body if method in ("POST", "PUT", "PATCH") else None
    return _request(
        method,
        path,
        organisation_id=organisation_id,
        user_id=user_id,
        content=body,
        content_type="application/json" if body is not None else None,
        content_length=len(body) if body is not None else None,
        timeout=JSON_TIMEOUT_S,
    )


def forward_chunk(
    request: HttpRequest, path: str, *, organisation_id: int, user_id: int
) -> HttpResponse:
    """Relay one chunk of a customer's file: read a piece, send a piece, write nothing.

    Two refusals before anything is read, and the second one is the non-obvious one.

    A chunk must declare its length: without one there is nothing to stream and no way to bound it.

    And a chunk must **not** arrive as a form. Django's CSRF check runs before this view and reads
    `request.POST` for every POST; for `application/octet-stream` that is an empty `QueryDict` and
    the body is untouched, but for `multipart/form-data` or `application/x-www-form-urlencoded` it
    *parses the body* — after which there is nothing left to stream, and a multipart parse is
    exactly the code path that writes a temporary file. The browser client always sends
    `application/octet-stream`; a request that does not is refused rather than relayed empty.
    """
    content_type = (request.META.get("CONTENT_TYPE") or "").split(";")[0].strip().lower()
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or content_type in FORM_CONTENT_TYPES:
        return HttpResponse(
            b'{"detail": "invalid_request", "message": ""}',
            status=400,
            content_type="application/json",
        )
    return _request(
        "POST",
        path,
        organisation_id=organisation_id,
        user_id=user_id,
        content=_pieces(request, length),
        content_type="application/octet-stream",
        content_length=length,
        timeout=CHUNK_TIMEOUT_S,
    )
