"""Invariants of the rendered production compose file (`make prod-config`, ADR 0028).

Reads `docker compose config --format json` on stdin. Standard library only: this runs on the
operator's machine and on the production host, neither of which has the backend virtualenv.
"""

import ipaddress
import json
import sys

EXPECTED_SERVICES = {"caddy", "customer", "db", "internal", "web"}
PUBLISHED_ONLY_BY = "caddy"
PUBLIC_PORTS = {80, 443}
# The purge site (ADR 0028) is published on production's Tailscale address, never a wildcard.
TAILSCALE_ONLY_PORTS = {8081}
WILDCARD_ADDRESSES = {"", "*", "0.0.0.0", "::", "[::]"}
# One host, one public IPv4, one pair of ports 80/443, two names (operator, 2026-09-19): this Caddy
# serves the customer app on APP_DOMAIN and, with PUBLIC_SITE=on, the marketing site on SITE_DOMAIN.
# A docker-published port bypasses the host firewall, so every address is named explicitly: a
# wildcard would publish the public edge on the tailnet as well, where the staff side lives.
TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)
# Loopback is the rehearsal on the development VPS (runbook section 3), never production.
LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)
# Caddy's own hardening. The standalone site edge has run with exactly this since 2026-09-18, and
# this process takes the public brochure over from it, so it must not be the weaker of the two.
ALLOWED_CAPABILITIES = {"NET_BIND_SERVICE"}
# The TLS issuers the Caddyfile has a snippet for (`import tls_{$CADDY_TLS}`). A third value does
# not adapt, so it is not a broken site block but a Caddy that never starts.
TLS_ISSUERS = {"acme", "internal"}
EXPECTED_SURFACE = {"web": "customer", "internal": "internal"}
# Production is unattended: after a reboot or an OOM kill it comes back without the operator.
RESTART_POLICIES = {"always", "unless-stopped"}
# The database talks to `web` and `internal` and to nothing else; `data` is the internal network.
DB_NETWORKS = {"data"}
# The VPS is 4 GB (runbook section 1). Every service is capped, and the caps together leave room
# for the host, a backup dump and a second image during a deploy.
MEMORY_BUDGET_MB = 3584
# Production holds no checkout (runbook section 4): the images are built on development and
# shipped. A bind mount out of the source tree is the development habit, and on production it
# mounts an empty directory over the application.
SOURCE_TREE_MARKERS = ("/backend/", "/backend:", "/frontend/", "/frontend:")
# Where the public marketing site (website/public) is mounted into the Caddy container.
WEBSITE_MOUNT = "/srv/site"
# Mail (ADR 0054 §1): production sends none, so it holds no transport and no SMTP credential. Even
# the backend name is refused — naming one here would be the first half of configuring a sender.
MAIL_TRANSPORT_KEYS = (
    "EMAIL_BACKEND",
    "EMAIL_HOST",
    "EMAIL_PORT",
    "EMAIL_HOST_USER",
    "EMAIL_HOST_PASSWORD",
    "EMAIL_USE_TLS",
    "EMAIL_USE_SSL",
    "ACCOUNT_MAIL_FROM",
)


def website_problems(services: dict) -> list[str]:
    """The public marketing site (website/), served by Caddy from static files.

    It is a brochure on its own hostname, not a surface: it must reach no upstream, add no port,
    and live in exactly one place — a read-only mount on `caddy`. `PUBLIC_SITE` resolves an
    `import` while Caddy adapts its config, so a third value is not a dark brochure but a Caddy
    that does not start, which takes the customer app with it. Catch the typo here.
    """
    found: list[str] = []
    caddy = services.get("caddy", {})
    public_site = (caddy.get("environment", {}) or {}).get("PUBLIC_SITE")
    if public_site not in ("on", "off"):
        found.append(f"PUBLIC_SITE must be `on` or `off`, got {public_site!r}")
    for name, service in services.items():
        for volume in service.get("volumes") or []:
            if str(volume.get("target", "")) != WEBSITE_MOUNT:
                continue
            if name != PUBLISHED_ONLY_BY:
                found.append(f"{name} mounts the website; only {PUBLISHED_ONLY_BY} serves it")
            elif not volume.get("read_only"):
                found.append(f"the website mount at {WEBSITE_MOUNT} must be read-only")
    if public_site == "on" and not any(
        str(volume.get("target", "")) == WEBSITE_MOUNT for volume in caddy.get("volumes") or []
    ):
        found.append(f"PUBLIC_SITE=on but nothing is mounted at {WEBSITE_MOUNT}")
    return found


#: Where uploaded bytes may live, and therefore what may never be mounted here (ADR 0044 § 3).
SPOOL_MARKER = "upload-spool"
#: Commands that decode a customer's file. Production has no ffmpeg in its image; this is the second
#: statement of the same rule, so a copied compose block cannot start a decoder here.
DECODER_COMMANDS = ("fingerprint_worker", "cloud_decoder")
#: The only service that may relay a customer's chunk: the customer surface, which is the only one a
#: customer's request reaches. `internal` faces development, holds production's own signing key and
#: has no business holding a credential towards development as well.
RELAY_SERVICE = "web"


def truthy(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def cloud_fingerprinting_problems(services: dict) -> list[str]:
    """Cloud fingerprinting on the public host (issue #59, ADR 0044 §9, amendment 2026-09-19).

    Until 2026-09-19 this was one blanket rule: the flag had to be `false` on every Django service.
    The operator's decision of 2026-09-19 is that the feature must be available in the private beta
    on `dash.marceline.studio`, because independent creators have no machine that can hash a 4K
    master — so the rule is now the **narrow** one, and it is the same rule
    `core/deploy.cloud_upload_problems` refuses to start a process against:

    - the flag may be set **only together with relay mode**, and only on `web`;
    - a relay needs its address and its own scoped credential, and only `web` may hold that token;
    - there is still **no spool** on this host: not a volume, not a setting, not a mount;
    - there is still **no decoder** here: no service runs the worker or the decoder module.

    What is not checked here, because a compose file cannot see it: that the image has no ffmpeg
    (`infra/prod/web.Dockerfile` says why), and that nothing writes a chunk on the way through
    (`marceline/tests/test_upload_relay.py`).
    """
    found: list[str] = []
    for name in EXPECTED_SURFACE:
        env = services.get(name, {}).get("environment", {}) or {}
        if "CLOUD_FINGERPRINTING_ENABLED" not in env:
            found.append(
                f"{name} must name CLOUD_FINGERPRINTING_ENABLED explicitly; a setting this file "
                "does not pass is silently empty in the container for ever"
            )
            continue
        enabled = truthy(env.get("CLOUD_FINGERPRINTING_ENABLED"))
        relay_mode = truthy(env.get("CLOUD_UPLOAD_RELAY_MODE"))
        if enabled and name != RELAY_SERVICE:
            found.append(
                f"{name} must run with CLOUD_FINGERPRINTING_ENABLED=false; only {RELAY_SERVICE} "
                "relays a customer's chunk (ADR 0044 §9)"
            )
        elif enabled and not relay_mode:
            found.append(
                f"{name} has CLOUD_FINGERPRINTING_ENABLED without CLOUD_UPLOAD_RELAY_MODE; on the "
                "public host uploads exist only as a relay, which stores nothing (ADR 0044 §9)"
            )
        elif enabled and not env.get("CLOUD_RELAY_BASE_URL"):
            found.append(
                f"{name} relays uploads but has no CLOUD_RELAY_BASE_URL (development's "
                "Tailscale-only listener)"
            )
        elif enabled and not env.get("CLOUD_RELAY_TOKEN"):
            found.append(
                f"{name} relays uploads but has no CLOUD_RELAY_TOKEN (a service token of scope "
                "upload_relay, never the export token)"
            )
        if env.get("CLOUD_UPLOAD_SPOOL_DIR"):
            found.append(
                f"{name} is given CLOUD_UPLOAD_SPOOL_DIR; the public host spools nothing, it relays"
            )
    for name, service in services.items():
        env = service.get("environment", {}) or {}
        if name != RELAY_SERVICE and env.get("CLOUD_RELAY_TOKEN"):
            found.append(
                f"{name} must not hold CLOUD_RELAY_TOKEN; only {RELAY_SERVICE} relays"
            )
        mounts = " ".join(str(item.get("target", "")) for item in service.get("volumes") or [])
        sources = " ".join(str(item.get("source", "")) for item in service.get("volumes") or [])
        if SPOOL_MARKER in mounts or SPOOL_MARKER in sources:
            found.append(f"{name} mounts an upload spool; production stores no customer media")
        command = " ".join(service.get("command") or [])
        for decoder in DECODER_COMMANDS:
            if decoder in command:
                found.append(
                    f"{name} runs {decoder}; production decodes no customer media (ADR 0044 § 4)"
                )
    return found


def _address(host_ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host_ip.strip("[]"))
    except ValueError:
        return None


def _in(address: object, networks: tuple) -> bool:
    return any(address in network for network in networks)


def bind_problems(caddy: dict, public_site: str) -> list[str]:
    """Which address each published port binds, and whether it can do its job there.

    Two addresses, two jobs (operator, 2026-09-19): 80/443 on the host's **public** IPv4, carrying
    both public names — the customer app on `APP_DOMAIN` and, with `PUBLIC_SITE=on`, the marketing
    site on `SITE_DOMAIN`; 8081 on the **Tailscale** address, carrying development's cache purges
    and its outbox pulls. There is one public IPv4 and one pair of ports on this host, which is why
    the two edges become one Caddy at the cut-over (docs/runbooks/production.md section 14).

    A docker-published port bypasses the host firewall, so a wildcard is refused outright: it would
    publish the public edge on the tailnet as well, and the Tailscale-only listener is the one thing
    here that trusts its network instead of a session. Loopback on 80/443 is the rehearsal on the
    development VPS (runbook section 3) and stays allowed, with the public-name rules relaxed for
    it — nothing it serves is on the internet.
    """
    found: list[str] = []
    ports = caddy.get("ports") or []
    expected_targets = PUBLIC_PORTS | TAILSCALE_ONLY_PORTS
    published_targets = {int(port.get("target", 0)) for port in ports}
    for missing in sorted(expected_targets - published_targets):
        found.append(f"caddy does not publish port {missing}; nothing would answer on it")
    public_addresses = [
        _address(str(port.get("host_ip", "")))
        for port in ports
        if int(port.get("target", 0)) in PUBLIC_PORTS
    ]
    # A rehearsal: the public ports are on loopback, so no name it serves is on the internet.
    rehearsal = bool(public_addresses) and all(
        address is not None and _in(address, LOOPBACK_NETWORKS) for address in public_addresses
    )
    for port in ports:
        target = int(port.get("target", 0))
        if target not in expected_targets:
            found.append(f"caddy publishes unexpected port {target}")
            continue
        host_ip = str(port.get("host_ip", ""))
        if host_ip in WILDCARD_ADDRESSES:
            found.append(
                f"caddy port {target} is published on {host_ip!r}: a docker-published port "
                "bypasses the host firewall, so the address is named explicitly (CADDY_BIND for "
                "80/443, TAILSCALE_BIND for the purge site)"
            )
            continue
        address = _address(host_ip)
        if address is None:
            found.append(f"caddy port {target} is published on {host_ip!r}, which is no address")
            continue
        if target in TAILSCALE_ONLY_PORTS and not (
            _in(address, TAILSCALE_NETWORKS) or _in(address, LOOPBACK_NETWORKS)
        ):
            found.append(
                f"caddy port {target} is published on {host_ip}; the purge and outbox listener is "
                "reached over Tailscale only (TAILSCALE_BIND, or loopback for a rehearsal)"
            )
        if target in PUBLIC_PORTS and _in(address, TAILSCALE_NETWORKS) and public_site == "on":
            found.append(
                f"caddy port {target} is published on the Tailscale address {host_ip} while "
                "PUBLIC_SITE=on: marceline.studio is a live, promoted public name, and bound there "
                "it is reachable by nobody"
            )
        published = str(port.get("published") or target)
        if (
            target in PUBLIC_PORTS
            and public_site == "on"
            and not rehearsal
            and published != str(target)
        ):
            found.append(
                f"caddy publishes port {target} as {published} while PUBLIC_SITE=on: Let's "
                "Encrypt validates on 80 and 443 only, so no certificate would be issued"
            )
    return found


def edge_problems(services: dict) -> list[str]:
    """The Caddy container: its issuer, its ACME contact and its own hardening.

    Since the cut-over one process serves both public names (operator, 2026-09-19), so what used to
    be the standalone brochure edge's invariants (infra/prod/site/check_config.py) are this
    container's invariants now.
    """
    found: list[str] = []
    caddy = services.get(PUBLISHED_ONLY_BY, {})
    environment = caddy.get("environment", {}) or {}
    public_site = str(environment.get("PUBLIC_SITE", ""))
    issuer = str(environment.get("CADDY_TLS", ""))
    if issuer not in TLS_ISSUERS:
        found.append(
            f"CADDY_TLS must be one of {sorted(TLS_ISSUERS)}, got {issuer!r}: the Caddyfile "
            "resolves `import tls_{$CADDY_TLS}` while adapting, so a third value is a Caddy that "
            "does not start at all"
        )
    public_addresses = [
        _address(str(port.get("host_ip", "")))
        for port in caddy.get("ports") or []
        if int(port.get("target", 0)) in PUBLIC_PORTS
    ]
    rehearsal = bool(public_addresses) and all(
        address is not None and _in(address, LOOPBACK_NETWORKS) for address in public_addresses
    )
    if public_site == "on" and issuer == "internal" and not rehearsal:
        found.append(
            "PUBLIC_SITE=on with CADDY_TLS=internal: CADDY_TLS is one variable for every site "
            "block in the Caddyfile, so the live marketing site would be served with Caddy's own "
            "CA and every browser would warn. Use `acme` (docs/runbooks/production.md section 14)"
        )
    if not str(environment.get("CADDY_ACME_EMAIL", "")).strip():
        found.append(
            "caddy has no CADDY_ACME_EMAIL: `email` with an empty value does not adapt, so the "
            "edge would not start — and Let's Encrypt would have nobody to warn when renewals fail"
        )
    # The brochure's own container was hardened when it was a stack of its own; the process that
    # takes it over must not be weaker.
    if not caddy.get("read_only"):
        found.append(
            "caddy's root filesystem must be read-only: it serves files and proxies requests, and "
            "writes only /data, /config and /tmp"
        )
    if "/tmp" not in {str(path) for path in caddy.get("tmpfs") or []}:
        found.append("caddy needs a tmpfs at /tmp once its root filesystem is read-only")
    if "ALL" not in {str(capability).upper() for capability in caddy.get("cap_drop") or []}:
        found.append("caddy must cap_drop ALL")
    extra = {
        str(capability).upper() for capability in caddy.get("cap_add") or []
    } - ALLOWED_CAPABILITIES
    if extra:
        found.append(f"caddy cap_add holds {sorted(extra)}; only {sorted(ALLOWED_CAPABILITIES)}")
    if "no-new-privileges:true" not in {str(option) for option in caddy.get("security_opt") or []}:
        found.append("caddy must run with no-new-privileges:true")
    if caddy.get("privileged"):
        found.append("privileged: an edge never is")
    # Certificates and the ACME account outlive a recreate, or every deploy orders new ones and
    # runs into Let's Encrypt's duplicate-certificate limit (5 per week per name).
    volumes = {str(volume.get("target", "")): volume for volume in caddy.get("volumes") or []}
    for target in ("/data", "/config"):
        volume = volumes.get(target)
        if volume is None or volume.get("type") != "volume":
            found.append(
                f"caddy needs a named volume at {target}: certificates that do not survive a "
                "recreate order a new one every deploy (5 duplicates a week per name)"
            )
    found += bind_problems(caddy, public_site)
    return found


def problems(config: dict) -> list[str]:
    found: list[str] = []
    services = config.get("services", {})
    if set(services) != EXPECTED_SERVICES:
        found.append(
            f"services must be exactly {sorted(EXPECTED_SERVICES)}, got {sorted(services)}"
        )
    for name, service in services.items():
        if name != PUBLISHED_ONLY_BY and service.get("ports"):
            found.append(f"{name} publishes ports; only {PUBLISHED_ONLY_BY} may")
        command = " ".join(service.get("command") or [])
        for forbidden in (
            "crawl_worker",
            "media_worker",
            "capture_worker",
            "scheduler",
        ):
            if forbidden in command:
                found.append(
                    f"{name} runs {forbidden}; production runs no crawler code"
                )
    found += edge_problems(services)
    for name, surface in EXPECTED_SURFACE.items():
        env = services.get(name, {}).get("environment", {})
        if env.get("DJANGO_ENV") != "prod":
            found.append(f"{name} must run with DJANGO_ENV=prod")
        if env.get("MARCELINE_ROLE") != "prod":
            found.append(f"{name} must run with MARCELINE_ROLE=prod")
        if env.get("DJANGO_PUBLIC_SURFACE") != surface:
            found.append(f"{name} must run with DJANGO_PUBLIC_SURFACE={surface}")
        if "CACHE_SIGNING_KEY_PATH" in env:
            found.append(
                f"{name} must not hold development's signing key; production verifies it"
            )
    found += cloud_fingerprinting_problems(services)
    # Onboarding by invite (issue #115, ADR 0052 §9): the redemption runs on the public host, so
    # both Django processes must be *passed* the switch and the published documents — a variable the
    # compose file never hands over can never be switched on however infra/prod/.env is set, and the
    # symptom would be a creator's invite link answering the neutral 404 for ever.
    for name in EXPECTED_SURFACE:
        env = services.get(name, {}).get("environment", {}) or {}
        for required in ("ONBOARDING_INVITES_ENABLED", "ONBOARDING_DOCUMENTS"):
            if required not in env:
                found.append(
                    f"{name} is not passed {required}, so onboarding by invite cannot be switched "
                    "on for it however infra/prod/.env is set (ADR 0052 §9)"
                )
    # Address verification and password reset (ADR 0054 §1): the routes run here, the **mail does
    # not**. This host has no transport and is given none — a credential-bearing link must be minted
    # and sent by the machine that holds the accounts, so a `EMAIL_HOST` or an SMTP password in
    # production's environment is a design violation, not a configuration choice. The switch itself
    # is passed in (off by default), like every other flag, or it could never be turned on.
    for name in EXPECTED_SURFACE:
        env = services.get(name, {}).get("environment", {}) or {}
        if "ACCOUNT_EMAIL_FLOWS_ENABLED" not in env:
            found.append(
                f"{name} is not passed ACCOUNT_EMAIL_FLOWS_ENABLED, so the reset and verification "
                "routes cannot be switched on for it however infra/prod/.env is set (ADR 0054)"
            )
    for name, service in services.items():
        env = service.get("environment", {}) or {}
        for forbidden in MAIL_TRANSPORT_KEYS:
            if forbidden in env:
                found.append(
                    f"{name} is passed {forbidden}; the public host sends no mail at all — a reset "
                    "link is minted and mailed by development (ADR 0054 §1)"
                )
    for name, service in services.items():
        mounts = " ".join(
            str(item.get("target", "")) for item in service.get("volumes") or []
        )
        if "upload-spool" in mounts:
            found.append(
                f"{name} mounts an upload spool; production stores no customer media"
            )
    # Production's own outbox key (ADR 0028 write path) belongs to the Tailscale-only listener.
    for name, service in services.items():
        env = service.get("environment", {}) or {}
        mounts = " ".join(
            str(item.get("target", "")) for item in service.get("volumes") or []
        )
        holds_key = "OUTBOX_SIGNING_KEY_PATH" in env or "prod-outbox.key" in mounts
        if name != "internal" and holds_key:
            found.append(
                f"{name} must not hold the outbox signing key; only internal signs"
            )
    # The customer app (ADR 0036) renders pages and relays writes; it holds no database role, no
    # key and no Django settings, and it must know the public name — a request answered with the
    # wrong Host is a session that belongs to the proxy and a login throttle counting it.
    customer = services.get("customer", {})
    customer_env = customer.get("environment", {}) or {}
    for required in ("CUSTOMER_PUBLIC_ORIGIN", "CUSTOMER_DJANGO_BASE_URL"):
        if not customer_env.get(required):
            found.append(f"customer must run with {required} set")
    for forbidden in (
        "POSTGRES_PASSWORD",
        "DJANGO_SECRET_KEY",
        "CACHE_EXPORT_TOKEN",
        "CACHE_SIGNING_KEY_PATH",
        "OUTBOX_SIGNING_KEY_PATH",
    ):
        if forbidden in customer_env:
            found.append(
                f"customer must not hold {forbidden}; it renders pages, nothing else"
            )
    customer_networks = set(customer.get("networks") or {})
    if "data" in customer_networks:
        found.append("customer must not reach the database network")
    caddy_networks = set(services.get("caddy", {}).get("networks") or {})
    if not customer_networks & caddy_networks:
        # Caddy is the only way in, so a customer container on no shared network is a /app that
        # answers 502 the moment CUSTOMER_APP=next selects it — and the rollback is the only fix.
        found.append(
            "customer and caddy must share a network; Caddy is the only way into /app"
        )
    # Which app answers /app (ADR 0036). The Caddyfile resolves `import customer_{$CUSTOMER_APP}`
    # while adapting its config, so a third value is not a broken /app but a Caddy that does not
    # start at all — the whole public site, not one path. Catch a typo here, and validate the file
    # on the host before the reload (docs/runbooks/production.md section 11).
    customer_app = (services.get("caddy", {}).get("environment", {}) or {}).get(
        "CUSTOMER_APP"
    )
    if customer_app not in ("next", "spa"):
        found.append(f"CUSTOMER_APP must be `next` or `spa`, got {customer_app!r}")
    found += website_problems(services)
    if not config.get("networks", {}).get("data", {}).get("internal"):
        found.append("the data network (web <-> db) must be internal")
    if "data" in (services.get("caddy", {}).get("networks") or {}):
        found.append("caddy must not reach the database network")
    # Nobody logs in to production for weeks at a time, so everything about it has to survive a
    # reboot, an OOM kill and a crash on its own.
    for name, service in services.items():
        if service.get("restart") not in RESTART_POLICIES:
            found.append(
                f"{name} has restart={service.get('restart')!r}; production is unattended, so "
                f"every service restarts by itself (one of {sorted(RESTART_POLICIES)})"
            )
    db_networks = set(services.get("db", {}).get("networks") or {})
    if db_networks != DB_NETWORKS:
        found.append(f"db must be on exactly {sorted(DB_NETWORKS)}, got {sorted(db_networks)}")
    # A 4 GB VPS: one service without a cap can take the whole machine down with it, and the
    # caps together have to leave the host room to breathe.
    total_mb = 0
    for name, service in services.items():
        limit = service.get("mem_limit")
        if not limit:
            found.append(f"{name} has no mem_limit; on a 4 GB VPS every service is capped")
            continue
        total_mb += int(limit) // (1024 * 1024)
    if total_mb > MEMORY_BUDGET_MB:
        found.append(
            f"the mem_limits add up to {total_mb} MB, above the {MEMORY_BUDGET_MB} MB budget for "
            "a 4 GB VPS (runbook section 1)"
        )
    for name, service in services.items():
        for volume in service.get("volumes") or []:
            if volume.get("type") != "bind":
                continue
            source = str(volume.get("source", ""))
            if not volume.get("read_only"):
                found.append(
                    f"{name} bind-mounts {volume.get('target')} writable; production mounts "
                    "configuration and keys read-only"
                )
            if any(marker in source for marker in SOURCE_TREE_MARKERS):
                found.append(
                    f"{name} bind-mounts {volume.get('target')} out of the source tree; "
                    "production has no checkout, so it would mount an empty directory"
                )
    # Error reporting (issue #37) is off while SENTRY_DSN is empty — but a process the compose
    # file never hands the variable to can never be switched on at all, and the two unattended
    # Django processes are the whole point of having it.
    for name in EXPECTED_SURFACE:
        if "SENTRY_DSN" not in (services.get(name, {}).get("environment") or {}):
            found.append(
                f"{name} is not passed SENTRY_DSN, so error reporting cannot be switched on for "
                "it however infra/prod/.env is set (issue #37)"
            )
    return found


def main() -> int:
    found = problems(json.load(sys.stdin))
    for problem in found:
        print(f"prod-config: {problem}", file=sys.stderr)
    if not found:
        print("prod-config: compose file renders and keeps its invariants")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
