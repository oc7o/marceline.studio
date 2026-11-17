"""The public production cache exposes the customer API and the health check, nothing else.

ADR 0028. Caddy enforces the same allowlist (`infra/prod/Caddyfile`, checked on demand by
`infra/prod/check-surface.sh`); these tests pin Django's own wall, which has to hold even when the
proxy is misconfigured.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, resolve
from django.urls.exceptions import Resolver404
from pytest_django.fixtures import Settings

from marceline.customer_cache import CACHED_ROUTES
from marceline.public_surface import customer_path_allowed, role_allows, surface_allows

STAFF_PATHS = (
    "/admin/",
    "/admin/login/",
    "/api/v1/docs",
    "/api/v1/openapi.json",
    "/api/v1/auth/me",
    "/api/v1/auth/login",
    "/api/v1/review/counts",
    "/api/v1/review/overview",
    "/api/v1/review/safety",
    "/api/v1/media/1/image",
    "/api/v1/originals",
    # The machine-to-machine API (ADR 0028) is never on the public surface.
    "/internal/v1/cache/purge",
    "/internal/v1/export/organisations/1",
)
TRICK_PATHS = (
    "/api/v1/app/../review/overview",
    "/api/v1/app/./../auth/me",
    "/api/v1/app/%2E%2E/review/overview",
    "/api/v1//app/me",
    "/api/v1/app//me",
    "/api/v1/app\\..\\review",
    "/api/v1/apps",
    "/api/v1/app",
    "/api/v1/health/",
    "/API/v1/app/me",
)


def _routes(patterns: list[URLPattern | URLResolver], prefix: str = "") -> Iterator[str]:
    """Every URL the project routes, with path parameters filled in with `1`."""
    for entry in patterns:
        piece = re.sub(r"<[^>]+>", "1", str(entry.pattern)).lstrip("^").rstrip("$")
        if isinstance(entry, URLResolver):
            yield from _routes(entry.url_patterns, prefix + piece)
        elif "(" not in piece and "\\" not in piece:
            yield "/" + prefix + piece


@pytest.fixture
def customer_surface(settings: Settings) -> None:
    settings.PUBLIC_SURFACE = "customer"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/health",
        "/api/v1/app/me",
        "/api/v1/app/cases/1",
        # The invite redemption is public since ADR 0052 §9 slice 2: the creator who holds the link
        # is not on the tailnet. It creates nothing here (`test_invites_on_production.py`).
        "/api/v1/app/invite/abcdefghijklmnop",
    ],
)
def test_customer_paths_are_allowed(path: str) -> None:
    assert customer_path_allowed(path)


@pytest.mark.parametrize("path", STAFF_PATHS + TRICK_PATHS)
def test_everything_else_is_refused(path: str) -> None:
    assert not customer_path_allowed(path)


@pytest.mark.django_db
@pytest.mark.usefixtures("customer_surface")
@pytest.mark.parametrize("path", STAFF_PATHS + TRICK_PATHS)
def test_customer_surface_answers_404_without_touching_the_session(path: str) -> None:
    for method in ("get", "post"):
        response = getattr(Client(), method)(path)

        assert response.status_code == 404
        assert response.content == b""
        assert "Set-Cookie" not in response.headers


@pytest.mark.django_db
@pytest.mark.usefixtures("customer_surface")
def test_every_routed_url_outside_the_customer_api_is_404() -> None:
    routes = sorted(set(_routes(get_resolver().url_patterns)))
    private = [path for path in routes if not customer_path_allowed(path)]

    # The walk found the real staff routes, not just paths that would 404 anyway.
    for path in ("/api/v1/review/overview", "/api/v1/originals", "/admin/"):
        assert path in private
    for path in private:
        try:
            resolve(path)
        except Resolver404:
            continue
        assert Client().get(path).status_code == 404, path


@pytest.mark.django_db
@pytest.mark.usefixtures("customer_surface")
def test_customer_api_still_answers() -> None:
    client = Client()

    assert client.get("/api/v1/health").json() == {"status": "ok"}
    session = client.get("/api/v1/app/auth/session")
    assert session.status_code == 200
    assert "csrftoken" in session.cookies
    assert client.get("/api/v1/app/me").status_code == 401


@pytest.mark.django_db
def test_full_surface_passes_staff_routes_through(settings: Settings) -> None:
    settings.PUBLIC_SURFACE = "full"

    assert Client().get("/api/v1/review/overview").status_code == 401


# --- the Tailscale-only listeners (ADR 0028) ----------------------------------------------------


@pytest.mark.parametrize(
    ("surface", "allowed", "refused"),
    [
        (
            "export",
            ("/internal/v1/export/accounts", "/internal/v1/export/organisations/1"),
            (
                "/api/v1/app/me",
                "/api/v1/health",
                "/admin/",
                "/internal/v1/cache/purge",
                "/internal/v1/export/../cache/purge",
                "/internal/v1/export//accounts",
            ),
        ),
        (
            "internal",
            ("/internal/v1/cache/purge", "/internal/v1/outbox/pull", "/internal/v1/outbox/ack"),
            (
                "/api/v1/app/me",
                "/api/v1/app/fingerprints",
                "/api/v1/health",
                "/admin/",
                "/internal/v1/export/accounts",
                "/internal/v1/cache/purge/",
                "/internal/v1/cache/purge/../purge",
                "/internal/v1/outbox/",
                "/internal/v1/outbox/pull/",
                "/internal/v1/outbox/../cache/purge",
            ),
        ),
    ],
)
def test_each_internal_listener_allows_only_its_own_api(
    surface: str, allowed: tuple[str, ...], refused: tuple[str, ...]
) -> None:
    for path in allowed:
        assert surface_allows(surface, path), path
    for path in refused:
        assert not surface_allows(surface, path), path


@pytest.mark.parametrize(
    ("role", "path", "allowed"),
    [
        ("dev", "/internal/v1/export/accounts", True),
        ("dev", "/internal/v1/cache/purge", False),
        ("prod", "/internal/v1/export/accounts", False),
        ("prod", "/internal/v1/cache/purge", True),
        ("prod", "/api/v1/app/me", True),
        # Development pulls customer writes from production, never the other way round.
        ("prod", "/internal/v1/outbox/pull", True),
        ("prod", "/internal/v1/outbox/ack", True),
        ("dev", "/internal/v1/outbox/pull", False),
        ("dev", "/internal/v1/outbox/ack", False),
        ("prod", "/api/v1/app/fingerprints", True),
        # Invoices are development-only until the cache export carries them (ADR 0032).
        ("dev", "/api/v1/app/invoices", True),
        ("dev", "/api/v1/app/invoices/1/pdf", True),
        ("prod", "/api/v1/app/invoices", False),
        ("prod", "/api/v1/app/invoices/1/pdf", False),
        # Notice reads and writes reach production too (bundle plus outbox, ADR 0046) — except
        # the sandbox send, the one path in the product that may hand a draft to a mail transport.
        ("dev", "/api/v1/app/notices", True),
        ("prod", "/api/v1/app/notices", True),
        ("prod", "/api/v1/app/notices/candidates/orig_1", True),
        ("prod", "/api/v1/app/notices/notice_1/export/txt", True),
        ("dev", "/api/v1/app/notices/sandbox-send/notice_1", True),
        ("prod", "/api/v1/app/notices/sandbox-send/notice_1", False),
        # Rights reads and writes do reach production (bundle plus outbox, ADR 0034); only the
        # proof upload stays development-only — the vault never enters a signed bundle.
        ("prod", "/api/v1/app/rights", True),
        ("prod", "/api/v1/app/rights/claimant", True),
        ("dev", "/api/v1/app/rights/proofs", True),
        ("prod", "/api/v1/app/rights/proofs", False),
        # Cloud fingerprinting (issue #59, ADR 0044): the rights vault's rule applied to something
        # far more sensitive. Customer media never reaches the public host — not the declaration,
        # not a chunk, not the status read.
        ("dev", "/api/v1/app/uploads", True),
        ("prod", "/api/v1/app/uploads", False),
        ("prod", "/api/v1/app/uploads/upl_0000", False),
        ("prod", "/api/v1/app/uploads/upl_0000/chunks/0", False),
        ("prod", "/api/v1/app/uploads/upl_0000/complete", False),
        # The customer's own data export (ADR 0045 amendment 2026-09-18, issue #85): production
        # holds no originals, rights records or notice drafts beyond its signed cache bundle.
        ("dev", "/api/v1/app/export", True),
        ("prod", "/api/v1/app/export", False),
    ],
)
def test_each_half_of_the_internal_api_lives_on_one_role(
    role: str, path: str, allowed: bool
) -> None:
    assert role_allows(role, path) is allowed


@pytest.mark.django_db
@pytest.mark.parametrize("surface", ["export", "internal"])
def test_every_routed_url_outside_an_internal_listener_is_404(
    settings: Settings, surface: str
) -> None:
    settings.PUBLIC_SURFACE = surface
    settings.MARCELINE_ROLE = "dev" if surface == "export" else "prod"
    routes = sorted(set(_routes(get_resolver().url_patterns)))

    for path in routes:
        if surface_allows(surface, path):
            continue
        try:
            resolve(path)
        except Resolver404:
            continue
        assert Client().get(path).status_code == 404, path


# --- what production may answer for itself (ADR 0028) -------------------------------------------

#: Customer GETs production answers from its **own** tables on purpose. Each is about the caller's
#: account, session or production-local state — never organisation data development holds, so an
#: answer from the local tables is the right answer rather than an empty one.
PRODUCTION_LOCAL_READS = frozenset(
    {
        "/api/v1/app/auth/session",
        "/api/v1/app/me",
        # The write outbox lives on production; development never has these rows (ADR 0028
        # amendment "The production write path", ADR 0046).
        "/api/v1/app/fingerprints/submissions",
        "/api/v1/app/notices/submissions",
        # The invite's state (ADR 0052 §9, slice 2). It is read from production's **own** invite
        # rows, which `pull_accounts` replicates like the accounts themselves — not from a bundle:
        # there is no organisation to key a bundle on yet, and the half-finished redemption belongs
        # to the host the creator is typing into. An empty answer here is not a misleading "nothing
        # found" but the one neutral 404 every failing token gets.
        "/api/v1/app/invite/{token}",
        # The two mailed links (ADR 0054). Read from production's **own** replicated token rows,
        # like the invite's state and for the same reason: they belong to the host the reader is
        # typing into, there is no organisation to key a bundle on, and what production does not
        # have is the one neutral 404 every failing link gets — never an empty list that reads as
        # data.
        "/api/v1/app/auth/password-reset/{token}",
        "/api/v1/app/auth/verify-email/{token}",
    }
)

#: Customer GETs whose **view** reads the signed bundle itself instead of leaving it to
#: `CustomerCacheMiddleware` (ADR 0046). One member: the notice download, because serving it
#: records an `exported` event, and a write belongs in a view rather than in a middleware. It is
#: not "answered from production's empty tables" either — that is what this classification is
#: about — so it is a fourth case, not an exemption; `test_cache_roundtrip.py` proves the bytes
#: come from the bundle.
CACHE_READING_VIEWS = frozenset({"/api/v1/app/notices/{notice_id}/export/{fmt}"})


def _customer_get_paths() -> list[str]:
    """Every customer GET the committed OpenAPI document declares.

    Read from `openapi.json` rather than the resolver, because that file is the API contract and
    `make check` already fails on drift — so a new customer read cannot reach production without
    passing through this list.
    """
    spec = json.loads((Path(__file__).resolve().parents[2] / "openapi.json").read_text())
    return sorted(
        path
        for path, operations in spec["paths"].items()
        if path.startswith("/api/v1/app/") and "get" in operations
    )


def _cache_route(path: str) -> str:
    """An OpenAPI path as `customer_cache.CACHED_ROUTES` spells a resolved route."""
    return path.lstrip("/").replace("{", "<").replace("}", ">")


def test_every_customer_read_is_cached_production_local_or_development_only() -> None:
    """On production a customer read must come from the signed bundle, be deliberately
    production-local, or answer 404 there.

    Production's database holds accounts, sessions and the cache and nothing else (ADR 0028), so a
    fourth case — a read of organisation data that is neither cached nor refused — answers from
    empty local tables, and an empty list reads to a customer as "nothing found". This is the
    check nobody has to remember when a router is added: `notices` (ADR 0035), `invoices`
    (ADR 0032) and `rights/proofs` (ADR 0034) each chose one of the three.
    """
    unclassified = [
        path
        for path in _customer_get_paths()
        if path not in PRODUCTION_LOCAL_READS
        and path not in CACHE_READING_VIEWS
        and _cache_route(path) not in CACHED_ROUTES
        and role_allows("prod", path)
    ]

    assert unclassified == [], (
        "these customer reads would be answered from production's empty tables; add each to "
        "customer_cache.CACHED_ROUTES (with export.document_keys and store.DOCUMENT_KEY), to "
        f"public_surface.ROLE_ONLY_PREFIXES['dev'], or to PRODUCTION_LOCAL_READS: {unclassified}"
    )


def test_the_classification_is_not_vacuous() -> None:
    """The walk really sees the customer reads, and each of the four cases really has members."""
    paths = _customer_get_paths()

    assert "/api/v1/app/cases/{original_id}" in paths and len(paths) > 10
    assert _cache_route("/api/v1/app/cases/{original_id}") in CACHED_ROUTES
    assert _cache_route("/api/v1/app/notices/{notice_id}") in CACHED_ROUTES
    assert not role_allows("prod", "/api/v1/app/invoices")
    assert not role_allows("prod", "/api/v1/app/notices/sandbox-send/notice_1")
    assert set(paths) >= PRODUCTION_LOCAL_READS
    assert set(paths) >= CACHE_READING_VIEWS
