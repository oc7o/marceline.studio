"""HTTP between the two machines, over Tailscale (ADR 0028). Nothing here ever logs a URL.

- production → development: `GET /internal/v1/export/organisations/{id}` and
  `GET /internal/v1/export/accounts`, with the scoped service token;
- development → production: `POST /internal/v1/cache/purge`, a signed envelope;
- development → production: `POST /internal/v1/outbox/pull` and `POST /internal/v1/outbox/ack`,
  signed envelopes to the same Tailscale-only listener (`CACHE_PURGE_BASE_URL`); the pull answers a
  batch of customer writes signed by production (ADR 0028 amendment "The production write path").

Plain `httpx` rather than `core/http.py`: that client is the crawler's polite fetcher (robots,
per-host delays, a crawler User-Agent), none of which applies to our own two machines.
Tests replace `transport` with an `httpx.MockTransport`.
"""

import httpx
from django.conf import settings

from delivery.envelope import MAX_ENVELOPE_BYTES

#: Synchronous fetch on a cache miss or expiry (ADR 0028: "a short timeout (3 s)").
FETCH_TIMEOUT_S = 3.0
#: A purge runs after a staff decision commits; it must not hold that request for long.
PURGE_TIMEOUT_S = 2.0

#: A batch of customer writes is up to about 22 MB on the wire (`delivery.outbox.BATCH_BYTES`, or
#: one 16 MiB original), over Tailscale.
OUTBOX_PULL_TIMEOUT_S = 60.0
OUTBOX_ACK_TIMEOUT_S = 10.0

EXPORT_PREFIX = "/internal/v1/export"
PURGE_PATH = "/internal/v1/cache/purge"
OUTBOX_PULL_PATH = "/internal/v1/outbox/pull"
OUTBOX_ACK_PATH = "/internal/v1/outbox/ack"

#: Test hook: an `httpx.MockTransport` here replaces the network for every call below.
transport: httpx.BaseTransport | None = None


class DeliveryError(Exception):
    """A call to the other machine failed. `code` is safe to log and to store."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _request(
    method: str,
    base_url: str,
    path: str,
    *,
    timeout: float,
    headers: dict[str, str],
    content: bytes | None = None,
) -> bytes:
    if not base_url:
        raise DeliveryError("not_configured")
    try:
        with httpx.Client(timeout=timeout, transport=transport, follow_redirects=False) as client:
            response = client.request(
                method, base_url.rstrip("/") + path, headers=headers, content=content
            )
    except httpx.TimeoutException as exc:
        raise DeliveryError("timeout") from exc
    except httpx.HTTPError as exc:
        raise DeliveryError("connection_failed") from exc
    if response.status_code == 404:
        raise DeliveryError("not_found")
    if response.status_code in (401, 403):
        raise DeliveryError("unauthorised")
    if response.status_code != 200:
        raise DeliveryError(f"status_{response.status_code}")
    if len(response.content) > MAX_ENVELOPE_BYTES:
        raise DeliveryError("too_large")
    return response.content


def _export(path: str) -> bytes:
    token = settings.CACHE_EXPORT_TOKEN
    if not token:
        raise DeliveryError("not_configured")
    return _request(
        "GET",
        settings.CACHE_EXPORT_BASE_URL,
        EXPORT_PREFIX + path,
        timeout=FETCH_TIMEOUT_S,
        headers={"Authorization": f"Bearer {token}"},
    )


def fetch_bundle(organisation_id: int) -> bytes:
    """The signed bundle of one organisation, as wire bytes (not yet verified)."""
    return _export(f"/organisations/{int(organisation_id)}")


def fetch_accounts() -> bytes:
    """The signed accounts document, as wire bytes (not yet verified)."""
    return _export("/accounts")


def post_purge(wire: bytes) -> None:
    """Deliver a signed purge to production's Tailscale-only listener."""
    _request(
        "POST",
        settings.CACHE_PURGE_BASE_URL,
        PURGE_PATH,
        timeout=PURGE_TIMEOUT_S,
        content=wire,
        headers={"Content-Type": "application/json"},
    )


def post_outbox_pull(wire: bytes) -> bytes:
    """Ask production for pending customer writes; returns its signed batch (not yet verified)."""
    return _request(
        "POST",
        settings.CACHE_PURGE_BASE_URL,
        OUTBOX_PULL_PATH,
        timeout=OUTBOX_PULL_TIMEOUT_S,
        content=wire,
        headers={"Content-Type": "application/json"},
    )


def post_outbox_ack(wire: bytes) -> None:
    """Tell production what became of pulled writes."""
    _request(
        "POST",
        settings.CACHE_PURGE_BASE_URL,
        OUTBOX_ACK_PATH,
        timeout=OUTBOX_ACK_TIMEOUT_S,
        content=wire,
        headers={"Content-Type": "application/json"},
    )
