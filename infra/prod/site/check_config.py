"""Invariants of the standalone marketing-site edge (`make prod-config`, operator 2026-09-18).

Reads `docker compose config --format json` on stdin, like its sibling
`infra/prod/check_config.py`, and answers the one question that matters about this stack: is it
still only a static file server on the public address, with no way into the application?

It is the counterpart of the customer stack's checker, not an extension of it: the two have
opposite shapes (that one must have five services and a database network, this one must have one
service and no application network at all), and a single file trying to be both would accept each
stack's mistakes in the other's name.

Standard library only: it runs on the operator's machine and on the production host, neither of
which has the backend virtualenv.
"""

import ipaddress
import json
import sys
from pathlib import Path

ONLY_SERVICE = "caddy"
# 80 and 443 (TCP and QUIC) and nothing else: a brochure needs no other port, and this project
# must never grow the customer stack's Tailscale purge site.
PUBLIC_PORTS = {80, 443}
WILDCARD_ADDRESSES = {"", "*", "0.0.0.0", "::", "[::]"}
# What makes the customer edge private is that it binds here. A public site bound to the tailnet
# is reachable by nobody, which is exactly the mistake that produced this second stack.
TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)
# The networks of the customer stack (infra/prod/compose.yaml). Joining one would give a static
# file server a route to gunicorn and Postgres.
APPLICATION_NETWORKS = {"edge", "data"}
RESTART_POLICIES = {"always", "unless-stopped"}
SITE_MOUNT = "/srv/site"
CADDYFILE_MOUNT = "/etc/caddy/Caddyfile"
# Certificates and the ACME account must outlive a recreate, or every deploy orders a new
# certificate and runs into Let's Encrypt's duplicate limit.
NAMED_VOLUME_TARGETS = {"/data", "/config"}
# It serves 300 KB of static files. A cap this low is also the alarm if someone ever adds an
# upstream, a log or a cache to it.
MEMORY_CAP_MB = 256
# Capabilities: bind 80/443 inside the container, nothing else.
ALLOWED_CAPABILITIES = {"NET_BIND_SERVICE"}
# Configuration of the application has no business in this project. A value here would mean
# someone is wiring the brochure into Django, which is what the separation exists to prevent.
FORBIDDEN_ENV_PREFIXES = ("DJANGO_", "POSTGRES_", "CACHE_", "OUTBOX_", "CUSTOMER_", "SENTRY_")
# The Caddyfile is a file server and a redirect. Any of these words in it means it has grown an
# upstream, an admin socket, a request log (the privacy notice says there is none) or a
# dependency on a snippet file that is not mounted.
FORBIDDEN_CADDY_DIRECTIVES = ("reverse_proxy", "php_fastcgi", "forward_auth", "import ")


def _addresses(port: dict) -> str:
    return str(port.get("host_ip", ""))


def port_problems(service: dict) -> list[str]:
    found: list[str] = []
    ports = service.get("ports") or []
    if not ports:
        found.append("the site edge publishes no port; nothing would answer on the internet")
    for port in ports:
        target = int(port.get("target", 0))
        if target not in PUBLIC_PORTS:
            found.append(f"port {target} is published; only {sorted(PUBLIC_PORTS)} may be")
        host_ip = _addresses(port)
        if host_ip in WILDCARD_ADDRESSES:
            found.append(
                f"port {target} is published on {host_ip!r}: a docker-published port bypasses "
                "the host firewall, so SITE_BIND must name the public address explicitly"
            )
            continue
        try:
            address = ipaddress.ip_address(host_ip.strip("[]"))
        except ValueError:
            found.append(f"port {target} is published on {host_ip!r}, which is not an address")
            continue
        if any(address in network for network in TAILSCALE_NETWORKS):
            found.append(
                f"port {target} is published on the Tailscale address {host_ip}; the public site "
                "must bind the public address (that mistake is why this stack exists)"
            )
    return found


def volume_problems(service: dict) -> list[str]:
    found: list[str] = []
    volumes = service.get("volumes") or []
    targets = {str(volume.get("target", "")): volume for volume in volumes}
    for target in (SITE_MOUNT, CADDYFILE_MOUNT):
        volume = targets.get(target)
        if volume is None:
            found.append(f"nothing is mounted at {target}")
        elif not volume.get("read_only"):
            found.append(f"the mount at {target} must be read-only")
    for target in NAMED_VOLUME_TARGETS:
        volume = targets.get(target)
        if volume is None or volume.get("type") != "volume":
            found.append(
                f"{target} must be a named volume: certificates that do not survive a recreate "
                "order a new one every deploy (Let's Encrypt allows 5 duplicates a week)"
            )
    for volume in volumes:
        if volume.get("type") == "bind" and not volume.get("read_only"):
            found.append(f"the bind mount at {volume.get('target')} is writable")
    return found


def hardening_problems(service: dict) -> list[str]:
    found: list[str] = []
    if not service.get("read_only"):
        found.append("the root filesystem must be read-only; it serves files, it writes none")
    if "ALL" not in {str(cap).upper() for cap in service.get("cap_drop") or []}:
        found.append("cap_drop must contain ALL")
    extra = {str(cap).upper() for cap in service.get("cap_add") or []} - ALLOWED_CAPABILITIES
    if extra:
        found.append(
            f"cap_add holds {sorted(extra)}; only {sorted(ALLOWED_CAPABILITIES)} is needed"
        )
    if service.get("privileged"):
        found.append("privileged: a static file server never is")
    if service.get("restart") not in RESTART_POLICIES:
        found.append(
            f"restart={service.get('restart')!r}: this host is unattended, so it must come back "
            f"by itself (one of {sorted(RESTART_POLICIES)})"
        )
    limit = service.get("mem_limit")
    if not limit:
        found.append("no mem_limit; it shares a 4 GB VPS with the customer stack")
    elif int(limit) // (1024 * 1024) > MEMORY_CAP_MB:
        found.append(
            f"mem_limit is {int(limit) // (1024 * 1024)} MB, above {MEMORY_CAP_MB} MB: a file "
            "server that needs more than that is not a file server any more"
        )
    if service.get("depends_on"):
        found.append("depends_on: this stack must come up alone, whatever the customer stack does")
    image = str(service.get("image", ""))
    if not image:
        found.append("no image; the host builds nothing, so the tag must be one it already holds")
    elif image.endswith(":latest"):
        found.append(f"image {image} would be pulled: name the tag the host holds (SITE_IMAGE_TAG)")
    if service.get("build"):
        found.append("build: production has no checkout and no build memory")
    return found


def network_problems(service: dict, config: dict) -> list[str]:
    found: list[str] = []
    if service.get("network_mode"):
        found.append(f"network_mode={service['network_mode']!r}: use the project's own network")
    declared = set(service.get("networks") or {})
    for name in sorted(declared & APPLICATION_NETWORKS):
        found.append(f"the site edge is on the application network {name!r}")
    for name, network in (config.get("networks") or {}).items():
        if (network or {}).get("external"):
            found.append(
                f"network {name!r} is external: this stack must reach no other project's network"
            )
    return found


def environment_problems(service: dict) -> list[str]:
    environment = service.get("environment") or {}
    return [
        f"{key} has no business in the site edge: it holds no application configuration"
        for key in sorted(environment)
        if key.startswith(FORBIDDEN_ENV_PREFIXES)
    ]


def caddyfile_problems(service: dict) -> list[str]:
    """Read the very file that will be mounted, when this runs where it exists.

    The compose file cannot say what the Caddyfile does, and the one thing that would turn this
    brochure into a way into the application is a `reverse_proxy` line in it.
    """
    source = next(
        (
            str(volume.get("source", ""))
            for volume in service.get("volumes") or []
            if str(volume.get("target", "")) == CADDYFILE_MOUNT
        ),
        "",
    )
    path = Path(source)
    if not source or not path.is_file():
        return []
    text = "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines())
    found = [
        f"{path.name} contains `{directive.strip()}`: the site edge has no upstream and no "
        "snippet file mounted"
        for directive in FORBIDDEN_CADDY_DIRECTIVES
        if directive in text
    ]
    if "admin off" not in text:
        found.append(f"{path.name} does not disable the admin endpoint (`admin off`)")
    for line in text.splitlines():
        if line.split()[:1] == ["log"]:
            found.append(
                f"{path.name} configures an access log; the published privacy notice says this "
                "server keeps none"
            )
    return found


def problems(config: dict) -> list[str]:
    services = config.get("services", {})
    if set(services) != {ONLY_SERVICE}:
        return [
            f"the site edge is exactly one service ({ONLY_SERVICE}), got {sorted(services)}: "
            "anything else belongs in the customer stack, not beside the brochure"
        ]
    service = services[ONLY_SERVICE]
    return [
        *port_problems(service),
        *volume_problems(service),
        *hardening_problems(service),
        *network_problems(service, config),
        *environment_problems(service),
        *caddyfile_problems(service),
    ]


def main() -> int:
    found = problems(json.load(sys.stdin))
    for problem in found:
        print(f"site-config: {problem}", file=sys.stderr)
    if found:
        return 1
    print("site-config: the standalone site edge renders and keeps its invariants")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
