"""Only our own frontend may use the customer API from a browser (ADR 0028).

The SPA is served from the same origin as the API, so the correct CORS configuration is **none**:
without an `Access-Control-Allow-Origin` header a browser refuses to hand a cross-origin response
to the page that asked, and refuses to send a non-simple cross-origin request at all. Unsafe
requests are additionally refused by CSRF protection (token *and* Origin check), and the session
cookie is `SameSite=Lax`, so another site cannot ride a customer's session.

None of this stops a non-browser client such as curl — authentication, organisation scoping and
the login throttle do that. A signed-in customer can always script their own session; that is
their own data.
"""

from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from marceline.api import api
from marceline.tests.app_helpers import add_member, client_for, make_organisation

EVIL = "https://evil.example"
CORS_HEADERS = (
    "Access-Control-Allow-Origin",
    "Access-Control-Allow-Credentials",
    "Access-Control-Allow-Methods",
    "Access-Control-Allow-Headers",
)
UNSAFE = ("post", "put", "patch", "delete")


def _customer_operations() -> list[tuple[str, str, dict[str, Any]]]:
    """(method, path with parameters filled, operation) for every customer API route."""
    schema = api.get_openapi_schema()
    operations = []
    for raw_path, methods in schema["paths"].items():
        if not raw_path.startswith("/api/v1/app/"):
            continue
        path = raw_path
        for method, operation in methods.items():
            for parameter in operation.get("parameters", []):
                if parameter.get("in") == "path":
                    path = path.replace("{" + parameter["name"] + "}", "1")
            operations.append((method, path, operation))
    return operations


def _example_body(operation: dict[str, Any]) -> dict[str, Any]:
    """A body that passes validation, so the CSRF check — not a 422 — decides the answer."""
    content = operation.get("requestBody", {}).get("content", {}).get("application/json")
    if not content:
        return {}
    schema = content["schema"]
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = api.get_openapi_schema()["components"]["schemas"][name]
    fillers = {"string": "x", "integer": 1, "number": 1, "boolean": False, "array": []}
    return {
        key: fillers.get(spec.get("type", "string"), "x")
        for key, spec in schema.get("properties", {}).items()
    }


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "member@example.test")


def test_there_are_customer_routes_to_check() -> None:
    methods = {(method, path) for method, path, _ in _customer_operations()}
    assert ("get", "/api/v1/app/me") in methods
    assert ("post", "/api/v1/app/auth/login") in methods


def test_no_cors_machinery_is_installed() -> None:
    installed = " ".join([*settings.INSTALLED_APPS, *settings.MIDDLEWARE]).lower()
    assert "cors" not in installed
    assert not any(name.startswith("CORS_") for name in dir(settings))


@pytest.mark.django_db
@pytest.mark.parametrize("surface", ["full", "customer"])
def test_cross_origin_requests_never_get_cors_headers(
    member: User, settings: Settings, surface: str
) -> None:
    settings.PUBLIC_SURFACE = surface
    client = client_for(member)

    for method, path, _ in _customer_operations():
        preflight = client.options(
            path,
            HTTP_ORIGIN=EVIL,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD=method.upper(),
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type, x-csrftoken",
        )
        actual = getattr(client, method)(path, HTTP_ORIGIN=EVIL)
        for response in (preflight, actual):
            for header in CORS_HEADERS:
                assert header not in response.headers, (method, path, header)


@pytest.mark.django_db
def test_every_unsafe_customer_endpoint_requires_the_csrf_token(member: User) -> None:
    unsafe = [op for op in _customer_operations() if op[0] in UNSAFE]
    assert unsafe, "no state-changing customer endpoint found"

    for method, path, operation in unsafe:
        client = Client(enforce_csrf_checks=True)
        client.force_login(member)
        response = getattr(client, method)(
            path, _example_body(operation), content_type="application/json"
        )
        assert response.status_code == 403, (method, path, response.status_code)


@pytest.mark.django_db
def test_a_valid_token_from_another_origin_is_still_refused(member: User) -> None:
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/v1/app/auth/session").cookies["csrftoken"].value
    body = {"username": "member@example.test", "password": "wrong-password"}

    foreign = client.post(
        "/api/v1/app/auth/login",
        body,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
        HTTP_ORIGIN=EVIL,
    )
    same_origin = client.post(
        "/api/v1/app/auth/login",
        body,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
        HTTP_ORIGIN="http://testserver",
    )

    assert foreign.status_code == 403
    assert same_origin.status_code == 401  # past CSRF; the password decides


def test_session_cookie_flags_keep_other_sites_out() -> None:
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.SESSION_COOKIE_SAMESITE in {"Lax", "Strict"}
    assert settings.CSRF_COOKIE_SAMESITE in {"Lax", "Strict"}
