"""No answer of the customer API may be stored by a cache (`marceline/no_store.py`).

Every one of them is about one organisation. A response with no `Cache-Control` at all is
heuristically cacheable, so "nobody has put a CDN in front of it yet" was the whole protection.
"""

import pytest
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import Client, RequestFactory

from accounts.models import Organisation
from marceline.no_store import NO_STORE, CustomerNoStoreMiddleware
from marceline.tests.app_helpers import (
    APP,
    add_member,
    client_for,
    make_organisation,
    public_id_of,
)
from matching.models import Reference


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "alpha-owner")


@pytest.mark.parametrize(
    "path",
    [
        f"{APP}/me",
        f"{APP}/originals",
        f"{APP}/findings",
        f"{APP}/cases",
        f"{APP}/removals",
        f"{APP}/invoices",
        f"{APP}/rights",
        f"{APP}/notices",
        f"{APP}/fingerprints/submissions",
        # Cloud fingerprinting (ADR 0044): an upload's status names the customer's own file. It
        # answers 404 here (the switch is off, and these routes are development-only), and a
        # refusal must be no more storable than an answer.
        f"{APP}/uploads",
        f"{APP}/auth/session",
    ],
)
def test_every_customer_read_refuses_to_be_stored(member: User, path: str) -> None:
    response = client_for(member).get(path)

    assert response.headers["Cache-Control"] == NO_STORE, path


def test_a_refusal_is_no_more_cacheable_than_an_answer(client: Client, db: None) -> None:
    """A 401 and a 404 are answers about this account too, and both used to carry nothing."""
    assert client.get(f"{APP}/me").headers["Cache-Control"] == NO_STORE
    assert client.get(f"{APP}/originals/orig_nope").headers["Cache-Control"] == NO_STORE


def test_an_endpoints_own_directive_is_never_overwritten() -> None:
    """The invoice PDF sets `private, no-store` for itself (ADR 0032) and a future endpoint may
    need something else of its own; this middleware only fills a gap."""
    own = HttpResponse()
    own["Cache-Control"] = "private, max-age=30"
    middleware = CustomerNoStoreMiddleware(lambda _request: own)

    answered = middleware(RequestFactory().get(f"{APP}/invoices/inv_x/pdf"))

    assert answered["Cache-Control"] == "private, max-age=30"


def test_a_route_outside_the_customer_api_is_not_touched() -> None:
    middleware = CustomerNoStoreMiddleware(lambda _request: HttpResponse())

    answered = middleware(RequestFactory().get("/api/v1/review/counts"))

    assert not answered.has_header("Cache-Control")


def test_the_invoice_pdfs_404_is_covered_by_the_middleware(member: User) -> None:
    """A malformed id never reaches the endpoint that sets its own directive, so the refusal is
    the middleware's to mark — and it is still an answer about this account."""
    response = client_for(member).get(f"{APP}/invoices/inv_nope/pdf")

    assert response.headers["Cache-Control"] == NO_STORE


def test_the_health_check_is_left_alone(client: Client, db: None) -> None:
    """Public, constant, and the container's own probe: caching it costs nobody anything."""
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert "Cache-Control" not in response.headers


def test_an_original_a_customer_owns_is_not_stored_either(member: User, db: None) -> None:
    from marceline.tests.app_helpers import make_reference

    reference: Reference = make_reference(member, title="Alpha's own original")

    response = client_for(member).get(f"{APP}/originals/{public_id_of(reference)}")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == NO_STORE
