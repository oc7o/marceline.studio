from collections.abc import Iterator

import pytest
from django.contrib.auth.models import User
from django.test import Client

from catalog.models import Creator
from marceline.review import media
from matching.models import Reference


@pytest.fixture
def staff_user(db: None) -> User:
    return User.objects.create_user("reviewer", password="secret-pass-123", is_staff=True)


@pytest.fixture
def staff_client(staff_user: User) -> Client:
    client = Client()
    client.force_login(staff_user)
    return client


@pytest.fixture
def user_client(db: None) -> Client:
    client = Client()
    client.force_login(User.objects.create_user("customer", password="secret-pass-123"))
    return client


@pytest.fixture
def reference(db: None) -> Reference:
    creator = Creator.objects.create(name="Synthetic Artist", aliases=["Test Alias"])
    return Reference.objects.create(
        creator=creator,
        title="Synthetic Artist mountain sunrise",
        duration_s=1000,
        sha256="a" * 64,
        frame_fps=1,
    )


@pytest.fixture(autouse=True)
def reset_image_rate_limit() -> Iterator[None]:
    media.limiter.reset()
    media.robots.clear()
    yield
    media.limiter.reset()
    media.robots.clear()
