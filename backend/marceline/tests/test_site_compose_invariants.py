"""The standalone site edge's checker refuses what it claims to refuse (operator, 2026-09-18).

`infra/prod/site/check_config.py` is the gate `make prod-config` puts in front of the second,
tiny stack that serves marceline.studio. It exists because the customer stack's own Caddy cannot
serve the brochure today: it binds 80/443 to the host's Tailscale address and issues internal
certificates for every site block in its file, because `dash.marceline.studio` is deliberately
still private (docs/runbooks/production.md §12).

The risk this checker carries is therefore specific: a file server on the public address, on the
same machine as gunicorn and Postgres, that must never grow a route to either. An invariant with
a typo in it passes silently for ever, so this walks the checker rule by rule — one rendering
that must be accepted, one mutation per rule that must be named.

Like its sibling for the customer stack it does not read the compose file itself (`make
prod-config` runs the real file through the real `docker compose`); the subject here is the
checker. It is standard-library only, because it also runs on the production host.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

from django.conf import settings

CHECK_CONFIG = settings.REPO_ROOT / "infra" / "prod" / "site" / "check_config.py"


def _problems() -> Callable[[dict[str, Any]], list[str]]:
    spec = importlib.util.spec_from_file_location("site_check_config", CHECK_CONFIG)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checker: Callable[[dict[str, Any]], list[str]] = module.problems
    return checker


def _rendered() -> dict[str, Any]:
    """What `docker compose config --format json` renders for a healthy site edge.

    The Caddyfile mount deliberately points at a path that does not exist: the file's own rules
    are exercised in test_site_edge_config.py against the real file, and a checker that silently
    skipped a missing file would otherwise never be tested for it.
    """
    return {
        "name": "marceline-site",
        "services": {
            "caddy": {
                "image": "marceline-prod-caddy:c24bd79",
                "restart": "unless-stopped",
                "read_only": True,
                "cap_drop": ["ALL"],
                "cap_add": ["NET_BIND_SERVICE"],
                "mem_limit": 128 * 1024**2,
                "networks": {"default": None},
                "environment": {"SITE_DOMAIN": "marceline.studio"},
                "ports": [
                    {"target": 80, "host_ip": "203.0.113.10", "protocol": "tcp"},
                    {"target": 443, "host_ip": "203.0.113.10", "protocol": "tcp"},
                    {"target": 443, "host_ip": "203.0.113.10", "protocol": "udp"},
                    {"target": 80, "host_ip": "2001:db8::10", "protocol": "tcp"},
                ],
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/srv/marceline/infra/site/Caddyfile.absent",
                        "target": "/etc/caddy/Caddyfile",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": "/srv/marceline/site",
                        "target": "/srv/site",
                        "read_only": True,
                    },
                    {"type": "volume", "source": "site_data", "target": "/data"},
                    {"type": "volume", "source": "site_config", "target": "/config"},
                ],
            }
        },
        "networks": {"default": {"name": "marceline-site_default"}},
    }


def test_a_healthy_site_edge_is_accepted() -> None:
    """Without this the tests below would pass on a checker that refuses everything."""
    assert _problems()(_rendered()) == []


def _refused(mutate: Callable[[dict[str, Any]], None]) -> str:
    config = _rendered()
    mutate(config)
    found = _problems()(config)
    assert found, "the checker accepted a site edge it must refuse"
    return " | ".join(found)


def test_a_second_service_is_refused() -> None:
    """The brochure's stack is one file server. Anything else belongs in the customer stack."""

    def mutate(config: dict[str, Any]) -> None:
        config["services"]["web"] = {"image": "marceline-prod-web:c24bd79"}

    assert "exactly one service" in _refused(mutate)


def test_a_wildcard_bind_is_refused() -> None:
    """A docker-published port bypasses the host firewall, so 0.0.0.0 publishes on the tailnet."""

    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["ports"][0]["host_ip"] = "0.0.0.0"

    assert "public address" in _refused(mutate)


def test_the_tailscale_address_is_refused() -> None:
    """The mistake this whole stack exists because of: a public name bound to 100.x."""

    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["ports"][0]["host_ip"] = "100.64.0.1"

    assert "Tailscale" in _refused(mutate)


def test_a_port_that_is_not_80_or_443_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["ports"].append({"target": 8081, "host_ip": "203.0.113.10"})

    assert "8081" in _refused(mutate)


def test_a_writable_site_mount_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["volumes"][1]["read_only"] = False

    assert "/srv/site" in _refused(mutate)


def test_certificates_in_an_anonymous_layer_are_refused() -> None:
    """A /data that does not survive a recreate orders a new certificate on every deploy."""

    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["volumes"] = [
            volume
            for volume in config["services"]["caddy"]["volumes"]
            if volume["target"] != "/data"
        ]

    assert "named volume" in _refused(mutate)


def test_an_application_network_is_refused() -> None:
    """The one thing that would turn a file server into a way into gunicorn and Postgres."""

    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["networks"]["edge"] = None

    assert "application network" in _refused(mutate)


def test_an_external_network_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["networks"]["shared"] = {"external": True, "name": "marceline-prod_edge"}

    assert "external" in _refused(mutate)


def test_application_configuration_in_the_site_edge_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["environment"]["POSTGRES_PASSWORD"] = "x"

    assert "no application configuration" in _refused(mutate)


def test_a_writable_root_filesystem_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["read_only"] = False

    assert "read-only" in _refused(mutate)


def test_keeping_the_capabilities_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["cap_drop"] = []

    assert "cap_drop" in _refused(mutate)


def test_an_extra_capability_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["cap_add"].append("SYS_ADMIN")

    assert "SYS_ADMIN" in _refused(mutate)


def test_a_service_that_does_not_restart_by_itself_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["restart"] = "no"

    assert "unattended" in _refused(mutate)


def test_a_memory_cap_a_file_server_cannot_need_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["mem_limit"] = 1024 * 1024**2

    assert "not a file server any more" in _refused(mutate)


def test_a_floating_image_tag_is_refused() -> None:
    """The host builds and pulls nothing: `latest` is a tag it does not have."""

    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["image"] = "marceline-prod-caddy:latest"

    assert "pulled" in _refused(mutate)


def test_building_on_the_production_host_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["build"] = {"context": "../../../frontend"}

    assert "no checkout" in _refused(mutate)


def test_waiting_on_the_customer_stack_is_refused() -> None:
    """The brochure must come up after a reboot whether or not Django does."""

    def mutate(config: dict[str, Any]) -> None:
        config["services"]["caddy"]["depends_on"] = {"web": {"condition": "service_healthy"}}

    assert "depends_on" in _refused(mutate)


def test_the_real_caddyfile_is_read_when_it_is_there() -> None:
    """With the real path mounted, the file's own rules are checked too."""
    config = _rendered()
    caddyfile = settings.REPO_ROOT / "infra" / "prod" / "site" / "Caddyfile"
    config["services"]["caddy"]["volumes"][0]["source"] = str(caddyfile)
    assert _problems()(config) == []


def test_an_upstream_in_the_caddyfile_is_refused(tmp_path: Path) -> None:
    """A `reverse_proxy` line is the whole difference between a brochure and a way in."""
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("{\n\tadmin off\n}\n\nsite.test {\n\treverse_proxy web:8000\n}\n")
    config = _rendered()
    config["services"]["caddy"]["volumes"][0]["source"] = str(caddyfile)
    assert "reverse_proxy" in " | ".join(_problems()(config))


def test_an_access_log_in_the_caddyfile_is_refused(tmp_path: Path) -> None:
    """The published privacy notice says this server keeps none."""
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("{\n\tadmin off\n}\n\nsite.test {\n\tlog\n\tfile_server\n}\n")
    config = _rendered()
    config["services"]["caddy"]["volumes"][0]["source"] = str(caddyfile)
    assert "privacy notice" in " | ".join(_problems()(config))
