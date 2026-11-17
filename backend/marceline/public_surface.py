"""What each listener answers — the second wall behind Caddy and Tailscale (ADR 0028).

Caddy already routes only `/api/v1/app/*` and `/api/v1/health` to Django on the public production
host (`infra/prod/Caddyfile`). This middleware is the second wall, so a proxy misconfiguration, a
container port published by mistake or a new router registered in `marceline/api.py` does not
put the staff review API, the Django admin, the media passthrough, the OpenAPI docs or the internal
API on the internet.

Surfaces (`DJANGO_PUBLIC_SURFACE`):
- `customer` — the public production cache: the customer API and the health check;
- `export` — development's Tailscale-only machine-to-machine listener: `/internal/v1/export/…` and
  the upload relay `/internal/v1/relay/…` (ADR 0044 §9) and nothing else;
- `internal` — production's Tailscale-only listener: `/internal/v1/cache/purge` and the outbox
  pull and acknowledgement (`/internal/v1/outbox/pull`, `/internal/v1/outbox/ack`) only;
- `full` — the development VPS over Tailscale: everything (a pass-through).

Each is an allowlist, not a blocklist: a route added later is private until someone adds it here.
Refused requests get the same bare 404 as an unknown path and never reach sessions, CSRF or the
database — the surface does not reveal what exists behind it.

On top of the surface, the **role** (`MARCELINE_ROLE`) keeps each half of the internal API on its
own machine whatever the surface: production never answers the export API, development never
accepts a purge or serves an outbox.
"""

import posixpath
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound

CUSTOMER_PREFIXES = ("/api/v1/app/",)
CUSTOMER_EXACT = frozenset({"/api/v1/health"})
EXPORT_PREFIX = "/internal/v1/export/"
#: Development's Tailscale-only upload relay (ADR 0044 §9, amendment 2026-09-19): the public host
#: forwards a customer's chunk here as it reads it, with a credential scoped to this prefix alone.
#: Served by the same `export` listener process — one Tailscale-only listener, two path prefixes and
#: two token scopes — and **development only**, like the export half it sits beside.
RELAY_PREFIX = "/internal/v1/relay/"
PURGE_PATH = "/internal/v1/cache/purge"
#: Development pulls and settles customer writes here (ADR 0028 amendment "The production write
#: path"); Caddy's Tailscale-only site forwards exactly these paths too.
OUTBOX_PATHS = frozenset({"/internal/v1/outbox/pull", "/internal/v1/outbox/ack"})
INTERNAL_PATHS = frozenset({PURGE_PATH}) | OUTBOX_PATHS
#: Paths only one role may answer. Invoices (ADR 0032) stay on development for now: production has
#: no invoice data and the signed cache carries no invoices or PDFs yet, so a production answer
#: would be an empty list that reads as "no invoices". Production answers the same bare 404 as for
#: an unknown path until the export carries them (ADR 0032, open decision). Rights (ADR 0034)
#: reads and writes now reach production (bundle export, outbox); only proof document uploads stay
#: development-only — the vault and its encrypted documents never enter a signed bundle. Notice
#: drafts (ADR 0035) reach production the same way since ADR 0046; only the sandbox send stays
#: here, which is why its path spells `sandbox-send` before the draft's id: this is a **prefix**
#: allowlist, and the one code path that may hand a notice to a mail transport must be refused on
#: the public host before anything routes.
ROLE_ONLY_PREFIXES = {
    "dev": (
        "/internal/v1/export/",
        "/internal/v1/relay/",
        "/api/v1/app/invoices",
        "/api/v1/app/rights/proofs",
        # Cloud fingerprinting (issue #59, ADR 0044): the customer's own video file. The public
        # host never *holds* customer media — not in a spool, not in the outbox, not in a
        # temporary file. Since the amendment of 2026-09-19 it may **relay** it: with
        # `CLOUD_UPLOAD_RELAY_MODE` the prefix leaves this list (see `dev_only_prefixes`) and the
        # bytes are forwarded to development as they are read. Without relay mode it stays here,
        # which is what keeps an accidental `CLOUD_FINGERPRINTING_ENABLED=true` on the public host
        # a 404 rather than a spool.
        "/api/v1/app/uploads",
        "/api/v1/app/notices/sandbox-send",
        # The customer's own data export (ADR 0045 amendment 2026-09-18, issue #85): the same
        # document `manage.py export_customer_data` writes. Production holds no originals, rights
        # records or notice drafts beyond its signed customer-cache bundle, so there is nothing
        # this route could add there.
        "/api/v1/app/export",
        # `/api/v1/app/invite` (issue #115, ADR 0052) was here until slice 2 was built on
        # 2026-09-19. It is a **public** customer surface now: the invite link has to work for a
        # creator who is not on the tailnet, which is the whole point of the private beta. Nothing
        # about ADR 0028 was loosened for it — production still creates no account. The last step
        # hands the redemption to `delivery.OutboxItem` and answers 202 (`marceline/app/invites.py`
        # `_hand_over`), development applies it with the same `complete()` its own endpoint runs,
        # and the signed accounts document carries the open invites' **token digests** so the public
        # host can refuse a bad token by itself (ADR 0052 §9, amendment 2026-09-19).
    ),
    "prod": ("/internal/v1/cache/", "/internal/v1/outbox/"),
}


def canonical(path: str) -> bool:
    """Only canonical paths qualify for any allowlist: `..`, `.`, empty segments, backslashes and
    percent-escapes are refused outright (no allowed route needs any of them), so
    `/api/v1/app/../review/…` cannot be argued into the allowlist by a later decoding step."""
    if "\\" in path or "//" in path or "%" in path:
        return False
    stripped = path.rstrip("/") or "/"
    return posixpath.normpath(stripped) == stripped


def customer_path_allowed(path: str) -> bool:
    """Whether a request path belongs to the public customer surface."""
    if not canonical(path):
        return False
    return path in CUSTOMER_EXACT or path.startswith(CUSTOMER_PREFIXES)


#: The one prefix whose role is a *configuration* question rather than a constant (ADR 0044 §9).
UPLOAD_PREFIX = "/api/v1/app/uploads"


def surface_allows(surface: str, path: str) -> bool:
    if surface == "full":
        return True
    if surface == "customer":
        return customer_path_allowed(path)
    if surface == "export":
        return canonical(path) and path.startswith((EXPORT_PREFIX, RELAY_PREFIX))
    if surface == "internal":
        return canonical(path) and path in INTERNAL_PATHS
    return False


def dev_only_prefixes(*, relay_mode: bool) -> tuple[str, ...]:
    """The development-only prefixes, given this process's relay setting.

    Exactly one entry moves: with `CLOUD_UPLOAD_RELAY_MODE` the upload prefix is no longer
    development-only, because the public host may then *forward* a chunk (ADR 0044 amendment
    2026-09-19) — it still stores nothing. Everything else is a constant.
    """
    prefixes = ROLE_ONLY_PREFIXES["dev"]
    if not relay_mode:
        return prefixes
    return tuple(prefix for prefix in prefixes if prefix != UPLOAD_PREFIX)


def role_allows(role: str, path: str, *, relay_mode: bool = False) -> bool:
    for other, prefixes in ROLE_ONLY_PREFIXES.items():
        if other == role:
            continue
        if other == "dev":
            prefixes = dev_only_prefixes(relay_mode=relay_mode)
        if path.startswith(prefixes):
            return False
    return True


class PublicSurfaceMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path_info
        if not surface_allows(settings.PUBLIC_SURFACE, path) or not role_allows(
            settings.MARCELINE_ROLE, path, relay_mode=bool(settings.CLOUD_UPLOAD_RELAY_MODE)
        ):
            return HttpResponseNotFound()
        return self.get_response(request)
