"""Production settings start only when they are safe, and then pass Django's deployment checklist.

Settings are read once per process, so each case runs `manage.py check --deploy` in a subprocess
with a controlled environment. Every variable the rules look at is set explicitly (to an empty
string where it should be unset), because `settings.py` fills gaps from the repository `.env` and
the main checkout has one.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]

SAFE_ENV = {
    "DJANGO_SETTINGS_MODULE": "marceline.settings",
    "DJANGO_ENV": "prod",
    "DJANGO_SECRET_KEY": "k7Qv2Lr9pXw4Zt8bNc1Hd6Fj3Gm5Ks0Ay_Ue-Io7Rq2Wl9Pz4Xv8Bn1Cm6Dh3Ej5Fg",
    "DJANGO_ALLOWED_HOSTS": "dash.marceline.studio",
    "DJANGO_CSRF_TRUSTED_ORIGINS": "https://dash.marceline.studio",
    "DJANGO_PUBLIC_SURFACE": "customer",
    "LOGIN_TRUSTED_PROXIES": "1",
    "POSTGRES_PASSWORD": "u8Jq3Zr6Tx1Lp9Wv4Nk",
    "SESSION_COOKIE_SECURE": "",
    "CSRF_COOKIE_SECURE": "",
    "SECURE_SSL_REDIRECT": "",
    "SECURE_HSTS_SECONDS": "",
    "SECURE_HSTS_INCLUDE_SUBDOMAINS": "",
    "SECURE_HSTS_PRELOAD": "",
    "SENTRY_DSN": "",
}
# HSTS preload commits the whole registrable domain; it stays an operator decision (ADR 0028).
ACCEPTED_DEPLOY_WARNINGS = {"security.W021"}


def _check_deploy(**overrides: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **SAFE_ENV, **overrides}
    return subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "ERROR"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_safe_production_settings_pass_the_deployment_checklist() -> None:
    result = _check_deploy()

    assert result.returncode == 0, result.stderr
    warnings = set(re.findall(r"\((security\.W\d+)\)", result.stdout + result.stderr))
    assert warnings <= ACCEPTED_DEPLOY_WARNINGS


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        ({"SESSION_COOKIE_SECURE": "0"}, "SESSION_COOKIE_SECURE"),
        ({"SECURE_SSL_REDIRECT": "false"}, "SECURE_SSL_REDIRECT"),
        ({"LOGIN_TRUSTED_PROXIES": "0"}, "LOGIN_TRUSTED_PROXIES"),
        ({"DJANGO_PUBLIC_SURFACE": ""}, "DJANGO_PUBLIC_SURFACE"),
        ({"POSTGRES_PASSWORD": "marceline"}, "POSTGRES_PASSWORD"),
        ({"DJANGO_SECRET_KEY": "change-me"}, "DJANGO_SECRET_KEY"),
    ],
)
def test_unsafe_production_settings_refuse_to_start(overrides: dict[str, str], named: str) -> None:
    result = _check_deploy(**overrides)

    assert result.returncode != 0
    assert "Unsafe production settings" in result.stderr
    assert named in result.stderr


def test_the_staff_pass_through_surface_refuses_to_start_on_production() -> None:
    """`full` answers the admin, the staff review API and the media passthrough (ADR 0028).

    On the production machine it would take away the second wall behind Caddy — the one that is
    there precisely for a misconfigured proxy or a port published by mistake — so the process
    must not start at all, however the environment got that value.
    """
    result = _check_deploy(DJANGO_PUBLIC_SURFACE="full")

    assert result.returncode != 0
    assert "DJANGO_PUBLIC_SURFACE=full needs MARCELINE_ROLE=dev" in result.stderr
