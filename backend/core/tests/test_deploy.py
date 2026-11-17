from dataclasses import replace
from typing import Any

import pytest

from core.deploy import ProductionSettings, production_problems, role_problems

SAFE = ProductionSettings(
    secret_key="k7Qv2Lr9pXw4Zt8bNc1Hd6Fj3Gm5Ks0Ay_Ue-Io7Rq2Wl9Pz4Xv8Bn1Cm6Dh3Ej5Fg",
    allowed_hosts=("dash.marceline.studio",),
    csrf_trusted_origins=("https://dash.marceline.studio",),
    session_cookie_secure=True,
    csrf_cookie_secure=True,
    ssl_redirect=True,
    hsts_seconds=31_536_000,
    trusted_proxies=1,
    database_password="u8Jq3Zr6Tx1Lp9Wv4Nk",
    public_surface="customer",
)


def test_safe_configuration_has_no_problems() -> None:
    assert production_problems(SAFE) == []


def test_full_surface_is_an_explicit_choice_not_a_problem() -> None:
    assert production_problems(replace(SAFE, public_surface="full")) == []


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"secret_key": "short"}, "DJANGO_SECRET_KEY"),
        ({"secret_key": "dev-insecure-secret-key" * 3}, "DJANGO_SECRET_KEY"),
        ({"secret_key": "django-insecure-" + "abcdefghij" * 5}, "DJANGO_SECRET_KEY"),
        ({"secret_key": "change-me-" + "abcdefghij" * 5}, "DJANGO_SECRET_KEY"),
        ({"secret_key": "a" * 60}, "DJANGO_SECRET_KEY"),
        ({"allowed_hosts": ()}, "DJANGO_ALLOWED_HOSTS"),
        ({"allowed_hosts": ("*",)}, "DJANGO_ALLOWED_HOSTS"),
        ({"allowed_hosts": ("*.marceline.test",)}, "DJANGO_ALLOWED_HOSTS"),
        (
            {"csrf_trusted_origins": ("http://dash.marceline.studio",)},
            "DJANGO_CSRF_TRUSTED_ORIGINS",
        ),
        ({"csrf_trusted_origins": ("https://*.marceline.test",)}, "DJANGO_CSRF_TRUSTED_ORIGINS"),
        ({"csrf_trusted_origins": ("https://partner.test",)}, "own hosts"),
        ({"session_cookie_secure": False}, "SESSION_COOKIE_SECURE"),
        ({"csrf_cookie_secure": False}, "CSRF_COOKIE_SECURE"),
        ({"ssl_redirect": False}, "SECURE_SSL_REDIRECT"),
        ({"hsts_seconds": 0}, "SECURE_HSTS_SECONDS"),
        ({"trusted_proxies": 0}, "LOGIN_TRUSTED_PROXIES"),
        ({"database_password": "marceline"}, "POSTGRES_PASSWORD"),
        ({"database_password": "Zr6Tx1Lp9W"}, "POSTGRES_PASSWORD"),
        ({"database_password": "change-me-change-me"}, "POSTGRES_PASSWORD"),
        ({"public_surface": None}, "DJANGO_PUBLIC_SURFACE"),
        ({"public_surface": "everything"}, "DJANGO_PUBLIC_SURFACE"),
    ],
)
def test_each_unsafe_value_is_named(change: dict[str, Any], fragment: str) -> None:
    problems = production_problems(replace(SAFE, **change))

    assert len(problems) == 1
    assert fragment in problems[0]


def test_problems_never_echo_the_secret_values() -> None:
    unsafe = replace(SAFE, secret_key="change-me-please", database_password="hunter2hunter2")

    text = " ".join(production_problems(unsafe))

    assert "change-me-please" not in text
    assert "hunter2" not in text


@pytest.mark.parametrize(
    ("role", "surface", "fragment"),
    [
        ("dev", "full", None),
        ("dev", "export", None),
        ("dev", None, None),
        ("prod", "customer", None),
        ("prod", "internal", None),
        # `full` is the pass-through that answers the admin and the staff review API. On the
        # production machine it would remove the second wall behind Caddy, so it is refused
        # there — production serves `customer` and `internal` and nothing else (ADR 0028).
        ("prod", "full", "needs MARCELINE_ROLE=dev"),
        ("dev", "customer", "needs MARCELINE_ROLE=prod"),
        ("dev", "internal", "needs MARCELINE_ROLE=prod"),
        ("prod", "export", "needs MARCELINE_ROLE=dev"),
        ("staging", "full", "MARCELINE_ROLE must be one of"),
    ],
)
def test_a_surface_belongs_to_one_role(
    role: str, surface: str | None, fragment: str | None
) -> None:
    problems = role_problems(role, surface, django_env="prod" if role == "prod" else "dev")

    if fragment is None:
        assert problems == []
    else:
        assert len(problems) == 1 and fragment in problems[0]


@pytest.mark.parametrize("django_env", ["dev", "", "production", "staging"])
def test_the_production_role_refuses_a_development_environment(django_env: str) -> None:
    """`MARCELINE_ROLE=prod` is what puts a process on the public machine; `DJANGO_ENV=prod` is
    what turns on `DEBUG=False`, a real secret key, `Secure` cookies and HSTS — and what makes
    `production_problems` run at all. They are two separate lines of `infra/prod/compose.yaml`, so
    they can disagree: the public host would then serve tracebacks and cookies without `Secure`,
    with none of the rules in this module ever consulted. The role has to bring the environment.
    """
    problems = role_problems("prod", "customer", django_env=django_env)

    assert len(problems) == 1
    assert "MARCELINE_ROLE=prod needs DJANGO_ENV=prod" in problems[0]


def test_the_development_role_is_free_to_run_either_environment() -> None:
    """Only the production role constrains the environment: development runs `DJANGO_ENV=dev`,
    and a hardened rehearsal of it is nobody's problem."""
    assert role_problems("dev", "full", django_env="dev") == []
    assert role_problems("dev", "full", django_env="prod") == []
