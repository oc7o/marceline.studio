import json
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client

REVIEW_GETS = (
    "/api/v1/review/counts",
    "/api/v1/review/references",
    "/api/v1/review/references/items",
    "/api/v1/review/clusters",
    "/api/v1/review/safety",
    "/api/v1/review/creators",
    "/api/v1/review/claims",
    "/api/v1/media/1/image",
)
CREDENTIALS = {"username": "reviewer", "password": "secret-pass-123"}


def post_json(client: Client, path: str, data: object, token: str | None = None) -> Any:
    headers = {"X-CSRFToken": token} if token else {}
    return client.post(path, json.dumps(data), content_type="application/json", headers=headers)


@pytest.mark.django_db
def test_me_sets_csrf_cookie_for_anonymous() -> None:
    response = Client().get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "username": None, "is_staff": False}
    assert "csrftoken" in response.cookies


@pytest.mark.django_db
def test_login_and_logout_require_csrf_token(staff_user: User) -> None:
    client = Client(enforce_csrf_checks=True)
    assert post_json(client, "/api/v1/auth/login", CREDENTIALS).status_code == 403

    token = client.get("/api/v1/auth/me").cookies["csrftoken"].value
    wrong = {**CREDENTIALS, "password": "nope"}
    assert post_json(client, "/api/v1/auth/login", wrong, token).status_code == 401

    response = post_json(client, "/api/v1/auth/login", CREDENTIALS, token)
    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "username": "reviewer", "is_staff": True}
    assert client.get("/api/v1/review/counts").status_code == 200

    token = client.cookies["csrftoken"].value  # rotated on login
    assert post_json(client, "/api/v1/auth/logout", {}).status_code == 403
    assert post_json(client, "/api/v1/auth/logout", {}, token).status_code == 200
    assert client.get("/api/v1/review/counts").status_code == 401


@pytest.mark.django_db
def test_login_refuses_non_staff() -> None:
    User.objects.create_user("customer", password="secret-pass-123")
    client = Client()
    credentials = {"username": "customer", "password": "secret-pass-123"}
    assert post_json(client, "/api/v1/auth/login", credentials).status_code == 403
    assert client.get("/api/v1/auth/me").json()["authenticated"] is False


@pytest.mark.django_db
@pytest.mark.parametrize("path", REVIEW_GETS)
def test_review_endpoints_require_staff(path: str, user_client: Client) -> None:
    assert Client().get(path).status_code == 401
    assert user_client.get(path).status_code == 403


@pytest.mark.django_db
def test_decisions_are_csrf_protected(staff_user: User) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(staff_user)
    payload = {"ids": [1], "decision": "confirm"}
    assert post_json(client, "/api/v1/review/references/leaks/decide", payload).status_code == 403


def test_counts_empty(staff_client: Client) -> None:
    response = staff_client.get("/api/v1/review/counts")
    assert response.status_code == 200
    assert response.json() == {
        "references": 0,
        "clusters": 0,
        "safety": 0,
        "safety_release": 0,
        "creators": 0,
        "claims": 0,
        "common_frames": 0,
    }
