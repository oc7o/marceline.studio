"""The production compose checker itself refuses what it claims to refuse (issue #37, ADR 0028).

`infra/prod/check_config.py` is the gate `make prod-config` puts in front of every deploy, and it
is the only thing that notices a production stack which publishes a database port, holds a signing
key in the public process or comes back from a reboot with half its services down. An invariant
with a typo in it passes silently for ever, and the file it reads is written by `docker compose
config`, so nothing else exercises it.

So this walks the checker: one rendered configuration that must be accepted, and one mutation per
rule that must be named. It deliberately does **not** read `infra/prod/compose.yaml` — resolving
that file's YAML anchors needs a parser the backend does not depend on, and `make prod-config`
already runs the real file through the real `docker compose`. Here the subject is the checker.

The checker is standard-library only (it also runs on the production host, which has no
virtualenv), so it is loaded from its path rather than imported.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from typing import Any

import pytest
from django.conf import settings

CHECK_CONFIG = settings.REPO_ROOT / "infra" / "prod" / "check_config.py"


def _problems() -> Callable[[dict[str, Any]], list[str]]:
    spec = importlib.util.spec_from_file_location("prod_check_config", CHECK_CONFIG)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checker: Callable[[dict[str, Any]], list[str]] = module.problems
    return checker


def _django_environment(surface: str) -> dict[str, str]:
    return {
        "DJANGO_ENV": "prod",
        "MARCELINE_ROLE": "prod",
        "DJANGO_PUBLIC_SURFACE": surface,
        "SENTRY_DSN": "",
        # ADR 0044: the public host stores no customer media, and says so rather than relying on
        # the setting's default. Since the amendment of 2026-09-19 `web` may relay an upload with
        # CLOUD_UPLOAD_RELAY_MODE; the accepted shape is `_relaying` below.
        "CLOUD_FINGERPRINTING_ENABLED": "false",
        # ADR 0052 §9: the redemption runs here, so both processes must be *passed* the switch and
        # the published documents — off and empty are fine, absent can never be switched on.
        "ONBOARDING_INVITES_ENABLED": "false",
        "ONBOARDING_DOCUMENTS": "",
        # ADR 0054: the reset and verification routes run here; the mail does not.
        "ACCOUNT_EMAIL_FLOWS_ENABLED": "false",
    }


def _rendered() -> dict[str, Any]:
    """What `docker compose config --format json` renders for a healthy production stack."""
    return {
        "services": {
            "db": {
                "restart": "unless-stopped",
                "mem_limit": 1024 * 1024**2,
                "networks": {"data": None},
                "volumes": [{"type": "volume", "source": "pgdata", "target": "/var/lib/pg"}],
            },
            "web": {
                "restart": "unless-stopped",
                "mem_limit": 768 * 1024**2,
                "networks": {"edge": None, "data": None},
                "environment": _django_environment("customer"),
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/srv/marceline/infra/prod/keys/dev-signing.pub",
                        "target": "/run/marceline/dev-signing.pub",
                        "read_only": True,
                    }
                ],
            },
            "internal": {
                "restart": "unless-stopped",
                "mem_limit": 384 * 1024**2,
                "networks": {"edge": None, "data": None},
                "environment": {
                    **_django_environment("internal"),
                    "OUTBOX_SIGNING_KEY_PATH": "/run/marceline/prod-outbox.key",
                },
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/srv/marceline/infra/prod/keys/prod-outbox.key",
                        "target": "/run/marceline/prod-outbox.key",
                        "read_only": True,
                    }
                ],
            },
            "customer": {
                "restart": "unless-stopped",
                "mem_limit": 384 * 1024**2,
                "networks": {"edge": None},
                "environment": {
                    "CUSTOMER_PUBLIC_ORIGIN": "https://app.example",
                    "CUSTOMER_DJANGO_BASE_URL": "http://web:8000",
                },
            },
            # The shape the cut-over gives it (operator, 2026-09-19): one Caddy, both public names,
            # the public IPv4 on 80/443 and the tailnet address on the purge site.
            "caddy": {
                "restart": "unless-stopped",
                "mem_limit": 256 * 1024**2,
                "networks": {"edge": None},
                "environment": {
                    "CUSTOMER_APP": "next",
                    "PUBLIC_SITE": "on",
                    "CADDY_TLS": "acme",
                    "CADDY_ACME_EMAIL": "contact@example.test",
                },
                "ports": [
                    {"target": 80, "published": "80", "host_ip": "203.0.113.10"},
                    {"target": 443, "published": "443", "host_ip": "203.0.113.10"},
                    {"target": 8081, "published": "8081", "host_ip": "100.64.0.1"},
                ],
                "read_only": True,
                "tmpfs": ["/tmp"],
                "cap_drop": ["ALL"],
                "cap_add": ["NET_BIND_SERVICE"],
                "security_opt": ["no-new-privileges:true"],
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/srv/marceline/infra/prod/Caddyfile",
                        "target": "/etc/caddy/Caddyfile",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": "/srv/marceline/site",
                        "target": "/srv/site",
                        "read_only": True,
                    },
                    {"type": "volume", "source": "caddy_data", "target": "/data"},
                    {"type": "volume", "source": "caddy_config", "target": "/config"},
                ],
            },
        },
        "networks": {"data": {"internal": True}, "edge": {}},
    }


def test_a_healthy_production_stack_is_accepted() -> None:
    """Without this the tests below would pass on a checker that refuses everything."""
    assert _problems()(_rendered()) == []


def _refused(mutate: Callable[[dict[str, Any]], None]) -> str:
    config = _rendered()
    mutate(config)
    found = _problems()(config)
    assert found, "the checker accepted a production stack it must refuse"
    return " | ".join(found)


def test_a_service_that_does_not_restart_by_itself_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["web"]["restart"] = "no"

    assert "unattended" in _refused(mutate)


def test_a_service_without_a_memory_cap_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        del config["services"]["db"]["mem_limit"]

    assert "mem_limit" in _refused(mutate)


def test_memory_caps_above_what_the_vps_has_are_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["db"]["mem_limit"] = 4096 * 1024**2

    assert "budget" in _refused(mutate)


def test_a_database_with_a_route_to_the_internet_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["db"]["networks"]["edge"] = None

    assert "db must be on exactly" in _refused(mutate)


def test_a_writable_bind_mount_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["internal"]["volumes"][0]["read_only"] = False

    assert "read-only" in _refused(mutate)


def test_a_bind_mount_out_of_the_source_tree_is_refused() -> None:
    """Production has no checkout: `../../backend:/app` would mount an empty directory."""

    def mutate(config: dict[str, Any]) -> None:
        config["services"]["web"]["volumes"].append(
            {
                "type": "bind",
                "source": "/home/deploy/marceline/backend/",
                "target": "/app",
                "read_only": True,
            }
        )

    assert "no checkout" in _refused(mutate)


def test_a_django_process_that_can_never_report_an_error_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        del config["services"]["internal"]["environment"]["SENTRY_DSN"]

    assert "SENTRY_DSN" in _refused(mutate)


def _relaying(config: dict[str, Any]) -> dict[str, str]:
    """Turn the rendered `web` into the one shape that is allowed to answer an upload route
    (ADR 0044 §9, amendment 2026-09-19): the flag, relay mode, an address and a scoped token."""
    env = config["services"]["web"]["environment"]
    env["CLOUD_FINGERPRINTING_ENABLED"] = "true"
    env["CLOUD_UPLOAD_RELAY_MODE"] = "true"
    env["CLOUD_RELAY_BASE_URL"] = "http://100.64.0.1:8082"
    env["CLOUD_RELAY_TOKEN"] = "mdx_relay"
    return env  # type: ignore[no-any-return]


def test_the_relay_shape_is_accepted() -> None:
    """The operator's decision of 2026-09-19: the feature must be available on the public host, as a
    relay. The gate's old blanket rule ("the flag is false here") would have refused this."""
    config = _rendered()
    _relaying(config)
    assert _problems()(config) == []


def test_the_flag_without_relay_mode_is_refused() -> None:
    """The obvious way to "make uploads work on production" is still the wrong one: the flag alone
    would mean a spool on the public host, which is what ADR 0028 exists to prevent."""

    def mutate(config: dict[str, Any]) -> None:
        config["services"]["web"]["environment"]["CLOUD_FINGERPRINTING_ENABLED"] = "true"

    assert "CLOUD_UPLOAD_RELAY_MODE" in _refused(mutate)


def test_a_relay_without_an_address_or_a_credential_is_refused() -> None:
    for missing, expected in (
        ("CLOUD_RELAY_BASE_URL", "CLOUD_RELAY_BASE_URL"),
        ("CLOUD_RELAY_TOKEN", "CLOUD_RELAY_TOKEN"),
    ):

        def mutate(config: dict[str, Any], missing: str = missing) -> None:
            _relaying(config)[missing] = ""

        assert expected in _refused(mutate)


def test_only_the_customer_surface_may_relay() -> None:
    """`internal` faces development and holds production's own signing key; it never sees a
    customer's chunk, so it may hold neither the flag nor the relay credential."""

    def flag(config: dict[str, Any]) -> None:
        config["services"]["internal"]["environment"]["CLOUD_FINGERPRINTING_ENABLED"] = "true"

    assert "only web relays a customer's chunk" in _refused(flag)

    def token(config: dict[str, Any]) -> None:
        config["services"]["internal"]["environment"]["CLOUD_RELAY_TOKEN"] = "mdx_relay"

    assert "must not hold CLOUD_RELAY_TOKEN" in _refused(token)


def test_the_customer_app_may_not_hold_the_relay_credential() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["customer"]["environment"]["CLOUD_RELAY_TOKEN"] = "mdx_relay"

    assert "must not hold CLOUD_RELAY_TOKEN" in _refused(mutate)


def test_a_flag_left_out_entirely_is_refused() -> None:
    """A setting the compose file does not name is silently empty in the container for ever
    (`marceline/tests/test_compose_env.py` exists because of that bug), so "off by omission" is not
    off: it is unstated."""

    def mutate(config: dict[str, Any]) -> None:
        del config["services"]["web"]["environment"]["CLOUD_FINGERPRINTING_ENABLED"]

    assert "must name CLOUD_FINGERPRINTING_ENABLED explicitly" in _refused(mutate)


def test_a_relay_with_a_spool_setting_is_refused() -> None:
    """A relay has no spool. This is the same rule `core/deploy.cloud_upload_problems` refuses to
    start a process against, stated where a deploy reads it."""

    def mutate(config: dict[str, Any]) -> None:
        _relaying(config)["CLOUD_UPLOAD_SPOOL_DIR"] = "/var/lib/marceline/upload-spool"

    assert "spools nothing" in _refused(mutate)


@pytest.mark.parametrize("name", ["ONBOARDING_INVITES_ENABLED", "ONBOARDING_DOCUMENTS"])
def test_a_process_that_could_never_take_a_redemption_is_refused(name: str) -> None:
    """ADR 0052 §9: an invite link that answers the neutral 404 for ever looks like a bad token.

    The symptom of a missing variable here is indistinguishable, to the creator and to the operator,
    from an expired invitation — so the deploy gate says it instead.
    """

    def mutate(config: dict[str, Any]) -> None:
        del config["services"]["web"]["environment"][name]

    assert name in _refused(mutate)


def test_a_production_stack_that_could_send_a_mail_is_refused() -> None:
    """ADR 0054 §1: a reset link is a credential, and the host that mints it holds the accounts.

    The obvious way to "make the reset mail work on production" is to put SMTP settings in
    `infra/prod/.env`. That must fail the deploy gate, not work — and the same for
    `ACCOUNT_MAIL_FROM`, which would be the first half of configuring a sender.
    """
    for key in ("EMAIL_HOST", "EMAIL_HOST_PASSWORD", "EMAIL_BACKEND", "ACCOUNT_MAIL_FROM"):

        def mutate(config: dict[str, Any], key: str = key) -> None:
            config["services"]["web"]["environment"][key] = "smtp.example.test"

        assert "sends no mail at all" in _refused(mutate), key


def test_a_process_that_could_never_take_a_reset_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        del config["services"]["internal"]["environment"]["ACCOUNT_EMAIL_FLOWS_ENABLED"]

    assert "ACCOUNT_EMAIL_FLOWS_ENABLED" in _refused(mutate)


def test_a_production_stack_with_an_upload_spool_is_refused() -> None:
    def mutate(config: dict[str, Any]) -> None:
        config["services"]["web"]["volumes"].append(
            {
                "type": "volume",
                "source": "upload-spool",
                "target": "/var/lib/marceline/upload-spool",
            }
        )

    assert "stores no customer media" in _refused(mutate)


def test_a_production_stack_that_runs_a_decoder_is_refused() -> None:
    """Relay mode moves the bytes through this host; it does not move the decode to it. There is no
    ffmpeg in the image (ADR 0044 § 4), and a copied compose block must not start one either."""

    def mutate(config: dict[str, Any]) -> None:
        config["services"]["web"]["command"] = ["python", "manage.py", "fingerprint_worker"]

    assert "decodes no customer media" in _refused(mutate)


def test_one_caddy_serving_both_names_keeps_its_bind_story() -> None:
    """The cut-over rules (operator, 2026-09-19): one public IPv4, one pair of ports, two names.

    `marceline.studio` is live and promoted; from the cut-over on it is served by this same Caddy
    (docs/runbooks/production.md section 14). Every way of getting that wrong is quiet — the edge
    comes up and answers on an address nobody can reach, or with a certificate no browser trusts —
    so each one is named here.
    """
    checks: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
        (
            "the public ports on a wildcard",
            lambda config: config["services"]["caddy"]["ports"][0].update(host_ip="0.0.0.0"),
            "bypasses the host firewall",
        ),
        (
            "the public ports on the tailnet while the brochure is served",
            lambda config: config["services"]["caddy"]["ports"][0].update(host_ip="100.64.0.1"),
            "reachable by nobody",
        ),
        (
            "the purge site on the public address",
            lambda config: config["services"]["caddy"]["ports"][2].update(host_ip="203.0.113.10"),
            "over Tailscale only",
        ),
        (
            "a public port Let's Encrypt never validates on",
            lambda config: config["services"]["caddy"]["ports"][1].update(published="8443"),
            "validates on 80 and 443 only",
        ),
        (
            "no public port at all",
            lambda config: config["services"]["caddy"].update(
                ports=[config["services"]["caddy"]["ports"][2]]
            ),
            "does not publish port 80",
        ),
        (
            "a third value for CADDY_TLS",
            lambda config: config["services"]["caddy"]["environment"].update(
                CADDY_TLS="letsencrypt"
            ),
            "must be one of",
        ),
        (
            "the live public site on Caddy's own CA",
            lambda config: config["services"]["caddy"]["environment"].update(CADDY_TLS="internal"),
            "every browser would warn",
        ),
        (
            "an ACME account with no contact address",
            lambda config: config["services"]["caddy"]["environment"].pop("CADDY_ACME_EMAIL"),
            "CADDY_ACME_EMAIL",
        ),
    ]
    for what, mutate, expected in checks:
        assert expected in _refused(mutate), what


def test_the_edge_that_serves_the_brochure_is_hardened_like_the_stack_it_replaces() -> None:
    """`infra/prod/site/` ran the brochure `read_only`, with every capability dropped, from
    2026-09-18 on. The process that takes it over must not be the weaker of the two, and the
    certificates must survive a recreate — 5 duplicates a week per name is the whole budget."""
    checks: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
        (
            "a writable root filesystem",
            lambda config: config["services"]["caddy"].update(read_only=False),
            "read-only",
        ),
        (
            "no tmpfs for /tmp",
            lambda config: config["services"]["caddy"].update(tmpfs=[]),
            "tmpfs at /tmp",
        ),
        (
            "capabilities kept",
            lambda config: config["services"]["caddy"].update(cap_drop=[]),
            "cap_drop ALL",
        ),
        (
            "a capability it does not need",
            lambda config: config["services"]["caddy"]["cap_add"].append("NET_ADMIN"),
            "cap_add holds",
        ),
        (
            "privilege escalation allowed",
            lambda config: config["services"]["caddy"].update(security_opt=[]),
            "no-new-privileges",
        ),
        (
            "certificates in the container's own filesystem",
            lambda config: config["services"]["caddy"]["volumes"].__setitem__(
                2, {"type": "bind", "source": "/srv/caddy", "target": "/data", "read_only": True}
            ),
            "named volume at /data",
        ),
    ]
    for what, mutate, expected in checks:
        assert expected in _refused(mutate), what


def test_the_rules_that_were_already_there_still_fire() -> None:
    """A regression net for the invariants this change did not write (ADR 0028)."""
    checks: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
        (
            "a published database port",
            lambda config: config["services"]["db"].update(
                ports=[{"target": 5432, "host_ip": "0.0.0.0"}]
            ),
            "only caddy may",
        ),
        (
            "the purge site on a wildcard address",
            lambda config: config["services"]["caddy"]["ports"][2].update(host_ip="0.0.0.0"),
            "bypasses the host firewall",
        ),
        (
            "a crawler in production",
            lambda config: config["services"]["web"].update(
                command=["python", "manage.py", "crawl_worker"]
            ),
            "no crawler code",
        ),
        (
            "development's signing key in production",
            lambda config: config["services"]["web"]["environment"].update(
                CACHE_SIGNING_KEY_PATH="/run/marceline/signing.key"
            ),
            "production verifies it",
        ),
        (
            "the outbox key in the public process",
            lambda config: config["services"]["web"]["environment"].update(
                OUTBOX_SIGNING_KEY_PATH="/run/marceline/prod-outbox.key"
            ),
            "only internal signs",
        ),
        (
            "the customer app on the database network",
            lambda config: config["services"]["customer"]["networks"].update(data=None),
            "database network",
        ),
        (
            "a third value for CUSTOMER_APP",
            lambda config: config["services"]["caddy"]["environment"].update(CUSTOMER_APP="vite"),
            "must be `next` or `spa`",
        ),
        (
            "a data network with a route out",
            lambda config: config["networks"]["data"].update(internal=False),
            "must be internal",
        ),
    ]
    for what, mutate, expected in checks:
        assert expected in _refused(mutate), what
