"""Refuse unsafe production configuration before Django serves a single request.

`marceline/settings.py` reads the environment; this module only judges the result. Pure (no Django
settings access, no environment reads), so every rule is a unit test rather than a subprocess.

Production means "behind Caddy on the public internet" (ADR 0005, ADR 0028). A setting that is
merely suboptimal there is not listed; everything here is a way to lose sessions, credentials or
the staff surface to a stranger, so a single problem stops the process from starting.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

# What the process exposes. `full` is the development / internal VPS (staff review API, admin,
# customer API); `customer` is the public production cache (ADR 0028), where only the customer
# API and the health check answer and everything else is a 404; `export` is development's
# Tailscale-only export listener and `internal` production's Tailscale-only purge listener.
PUBLIC_SURFACES = ("full", "customer", "export", "internal")
# Which machine of ADR 0028 a process belongs to, and the surfaces only that machine may serve.
# `full` is development's: it is the pass-through that answers the admin, the staff review API and
# the media passthrough. On the production machine that would take away the second wall behind
# Caddy (`marceline/public_surface.py`) — the very wall that is there for a misconfigured proxy or
# a port published by mistake — so `MARCELINE_ROLE=prod` may not serve it. Production's own
# surfaces are `customer` (the public cache) and `internal` (its Tailscale-only listener), and
# `infra/prod/compose.yaml` sets exactly those.
ROLES = ("dev", "prod")
#: The value of `DJANGO_ENV` that turns on every hardened default (`marceline/settings.py`) and
#: makes `production_problems` run at all. `MARCELINE_ROLE=prod` requires it.
PROD_ENV = "prod"
SURFACES_OF_ROLE = {
    "dev": frozenset({"full", "export"}),
    "prod": frozenset({"customer", "internal"}),
}

MIN_SECRET_KEY_LENGTH = 50
MIN_SECRET_KEY_UNIQUE_CHARACTERS = 5
MIN_DATABASE_PASSWORD_LENGTH = 16
# Values that are clearly placeholders or development defaults (.env.example, infra/compose.yaml).
PLACEHOLDER_MARKERS = ("change-me", "changeme", "insecure", "placeholder", "example")
WEAK_DATABASE_PASSWORDS = frozenset({"marceline", "postgres", "password"})


@dataclass(frozen=True)
class ProductionSettings:
    """The subset of settings whose wrong value in production is a security problem."""

    secret_key: str
    allowed_hosts: Sequence[str]
    csrf_trusted_origins: Sequence[str]
    session_cookie_secure: bool
    csrf_cookie_secure: bool
    ssl_redirect: bool
    hsts_seconds: int
    trusted_proxies: int
    database_password: str
    public_surface: str | None


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def production_problems(config: ProductionSettings) -> list[str]:
    """Every reason this configuration must not run in production; empty means acceptable."""
    problems: list[str] = []

    key = config.secret_key
    if (
        len(key) < MIN_SECRET_KEY_LENGTH
        or len(set(key)) < MIN_SECRET_KEY_UNIQUE_CHARACTERS
        or key.startswith("django-insecure")
        or _looks_like_placeholder(key)
    ):
        problems.append(
            f"DJANGO_SECRET_KEY must be a random value of at least {MIN_SECRET_KEY_LENGTH} "
            "characters, not a placeholder"
        )

    if not config.allowed_hosts:
        problems.append("DJANGO_ALLOWED_HOSTS must name the public hostname")
    elif any(host == "*" or host.startswith("*") for host in config.allowed_hosts):
        problems.append("DJANGO_ALLOWED_HOSTS must not contain wildcards")

    insecure_origins = [o for o in config.csrf_trusted_origins if not o.startswith("https://")]
    if insecure_origins:
        problems.append("DJANGO_CSRF_TRUSTED_ORIGINS must list https:// origins only")
    if any("*" in origin for origin in config.csrf_trusted_origins):
        problems.append("DJANGO_CSRF_TRUSTED_ORIGINS must not contain wildcards")
    # Only the app's own origin may send unsafe requests (ADR 0028): a trusted origin on another
    # host is exactly the cross-site caller CSRF protection exists to refuse.
    foreign = [
        origin
        for origin in config.csrf_trusted_origins
        if urlsplit(origin).hostname not in {host.lower() for host in config.allowed_hosts}
    ]
    hosts_usable = config.allowed_hosts and not any("*" in h for h in config.allowed_hosts)
    origins_usable = not any("*" in origin for origin in config.csrf_trusted_origins)
    if foreign and hosts_usable and origins_usable:
        problems.append("DJANGO_CSRF_TRUSTED_ORIGINS may only name the app's own hosts")

    if not config.session_cookie_secure:
        problems.append("SESSION_COOKIE_SECURE must not be turned off in production")
    if not config.csrf_cookie_secure:
        problems.append("CSRF_COOKIE_SECURE must not be turned off in production")
    if not config.ssl_redirect:
        problems.append("SECURE_SSL_REDIRECT must not be turned off in production")
    if config.hsts_seconds <= 0:
        problems.append("SECURE_HSTS_SECONDS must be positive in production")

    # Production runs behind Caddy (ADR 0005). With 0 every login failure is counted against the
    # proxy's address, so twenty wrong passwords from anyone lock out every customer at once.
    if config.trusted_proxies < 1:
        problems.append("LOGIN_TRUSTED_PROXIES must be 1 behind Caddy (the number of proxies)")

    password = config.database_password
    if (
        len(password) < MIN_DATABASE_PASSWORD_LENGTH
        or password.lower() in WEAK_DATABASE_PASSWORDS
        or _looks_like_placeholder(password)
    ):
        problems.append(
            f"POSTGRES_PASSWORD must be a random value of at least "
            f"{MIN_DATABASE_PASSWORD_LENGTH} characters, not a development default"
        )

    # No default in production: exposing the staff surface publicly must be a written decision.
    if config.public_surface is None:
        problems.append("DJANGO_PUBLIC_SURFACE must be set explicitly in production")
    elif config.public_surface not in PUBLIC_SURFACES:
        problems.append(f"DJANGO_PUBLIC_SURFACE must be one of {', '.join(PUBLIC_SURFACES)}")

    return problems


def role_problems(role: str, public_surface: str | None, *, django_env: str) -> list[str]:
    """A role that does not exist, a surface that belongs to the other machine (ADR 0028), or the
    production machine running with a development environment.

    Checked in every environment, not only production: an export listener started with the
    production role would answer nothing, and a purge listener with the development role would
    accept nothing — both are configuration mistakes worth refusing loudly.

    The last rule is the one that matters most on the public host. `production_problems` above is
    gated on `DJANGO_ENV=prod` and so is every hardened default it checks — `DEBUG`, the secret
    key, `Secure` cookies, HSTS. `MARCELINE_ROLE=prod` is what actually puts a process on the
    public machine, and the two are set by different lines of `infra/prod/compose.yaml`, so a
    process could be the production one and still be configured as a development one: tracebacks
    and session cookies without `Secure`, on the internet, with none of the checks above ever
    running. The role is the fact; the environment has to agree with it.
    """
    if role not in ROLES:
        return [f"MARCELINE_ROLE must be one of {', '.join(ROLES)}"]
    for other, surfaces in SURFACES_OF_ROLE.items():
        if other != role and public_surface in surfaces:
            return [f"DJANGO_PUBLIC_SURFACE={public_surface} needs MARCELINE_ROLE={other}"]
    if role == "prod" and django_env != PROD_ENV:
        return [f"MARCELINE_ROLE=prod needs DJANGO_ENV={PROD_ENV}"]
    return []


@dataclass(frozen=True)
class CloudUploadSettings:
    """The cloud-fingerprinting settings whose wrong combination would put a customer's video
    somewhere it may never be (ADR 0044, amendment 2026-09-19)."""

    role: str
    public_surface: str | None
    enabled: bool
    relay_mode: bool
    spool_dir: str
    relay_base_url: str
    relay_token: str


def cloud_upload_problems(config: CloudUploadSettings) -> list[str]:
    """Why this process must not start with these cloud-fingerprinting settings.

    The public host has one way to answer an upload route: **as a relay**, and a relay has no
    spool. Until 2026-09-19 the rule was simply "not on the public host"; it is narrower now and
    enforced in three places that cannot disagree — here (the process refuses to start),
    `marceline/public_surface.py` (the routes stay 404 without relay mode) and
    `infra/prod/check_config.py` (the compose file cannot hand the flag to a process without it).

    Checked in every environment, like `role_problems`: a development process in relay mode would
    point at itself, and a production process with a spool is the thing ADR 0028 exists to prevent.
    """
    problems: list[str] = []
    if config.relay_mode:
        if config.role == "dev":
            problems.append(
                "CLOUD_UPLOAD_RELAY_MODE belongs to the public host only: development is where the "
                "bytes land, so a relay there would point at itself"
            )
        if config.public_surface == "internal":
            problems.append(
                "CLOUD_UPLOAD_RELAY_MODE must not be set on the internal listener; only the "
                "customer surface ever receives a customer's chunk"
            )
        if config.spool_dir:
            problems.append(
                "a relay has no spool: CLOUD_UPLOAD_SPOOL_DIR must be empty when "
                "CLOUD_UPLOAD_RELAY_MODE is set"
            )
        if config.enabled and not config.relay_base_url:
            problems.append("CLOUD_RELAY_BASE_URL must name development's listener in relay mode")
        if config.enabled and not config.relay_token:
            problems.append(
                "CLOUD_RELAY_TOKEN must hold the scoped relay credential in relay mode "
                "(`manage.py create_service_token --scope upload_relay` on development)"
            )
    if config.role == "prod" and config.enabled:
        if not config.relay_mode:
            problems.append(
                "CLOUD_FINGERPRINTING_ENABLED may only be set on the public host together with "
                "CLOUD_UPLOAD_RELAY_MODE: customer media is never stored here (ADR 0044 §9)"
            )
        if config.spool_dir:
            problems.append(
                "CLOUD_UPLOAD_SPOOL_DIR must be empty on the public host; the bytes never rest here"
            )
    return problems
