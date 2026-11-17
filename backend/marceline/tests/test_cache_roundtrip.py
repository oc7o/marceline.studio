"""Development exports, production serves: the same bytes, the same tenancy (ADR 0028).

One database plays both machines here: `MARCELINE_ROLE` switches which side a request is on, and
the fetch production makes is wired straight to development's export code (or through its HTTP
endpoint with an `httpx.MockTransport`). No network, no real keys.
"""

import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Membership, Organisation
from delivery import client as delivery_client
from delivery import store
from delivery.envelope import FORMAT_ACCOUNTS, open_envelope
from delivery.models import CachedBundle, ServiceToken
from delivery.purge import apply_purge, verify_purge
from delivery.tests.helpers import KeyPair, make_keys, purge_wire
from delivery.tokens import create_token
from index.models import Page
from marceline import customer_cache, export
from marceline.accounts_replica import apply_accounts, verify_accounts
from marceline.api import api
from marceline.notices import export as notice_export
from marceline.notices import services as notice_services
from marceline.public_surface import role_allows
from marceline.tests.app_helpers import (
    APP,
    UNKNOWN_ORIGINAL,
    add_member,
    client_for,
    make_organisation,
    make_reference,
    public_id_of,
)
from matching.models import Reference, ReferenceLeak

EXPORT = "/internal/v1/export"


@pytest.fixture
def keys(tmp_path: Path, settings: Settings) -> KeyPair:
    pair = make_keys(tmp_path)
    settings.CACHE_SIGNING_KEY_PATH = str(pair.private_path)
    settings.CACHE_VERIFY_KEY_PATH = str(pair.public_path)
    return pair


@pytest.fixture
def alpha(db: None) -> Organisation:
    return make_organisation("Alpha Studio", Organisation.Segment.ENTERPRISE)


@pytest.fixture
def owner_a(alpha: Organisation) -> User:
    return add_member(alpha, "alpha-owner")


@pytest.fixture
def owner_b(db: None) -> User:
    return add_member(make_organisation("Bravo Creator"), "bravo-owner")


@pytest.fixture
def original_a(owner_a: User) -> Reference:
    return make_reference(owner_a, title="Alpha's own original")


@pytest.fixture
def finding_a(original_a: Reference, make_page: Callable[[str], Page]) -> Page:
    page = make_page("alpha-leak")
    page.availability = Page.Availability.REMOVED
    page.availability_checked_at = timezone.now()
    page.save(update_fields=["availability", "availability_checked_at"])
    ReferenceLeak.objects.create(reference=original_a, page=page, status="confirmed")
    return page


@pytest.fixture
def direct_fetch(keys: KeyPair, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Production's fetch, wired to development's export code in the same process."""
    calls: list[int] = []

    def fetch(organisation_id: int) -> bytes:
        calls.append(organisation_id)
        wire = export.signed_bundle(organisation_id, now=timezone.now())
        if wire is None:
            raise delivery_client.DeliveryError("not_found")
        return wire

    monkeypatch.setattr(delivery_client, "fetch_bundle", fetch)
    return calls


def customer_paths(original: Reference) -> list[str]:
    return [
        f"{APP}/originals",
        f"{APP}/originals/{public_id_of(original)}",
        f"{APP}/originals/{UNKNOWN_ORIGINAL}",
        f"{APP}/findings",
        f"{APP}/cases",
        f"{APP}/cases/{public_id_of(original)}",
        f"{APP}/cases/{UNKNOWN_ORIGINAL}",
        f"{APP}/removals",
        # Old integer URLs and malformed ids: the same 404, from the bundle's `*` document on prod.
        f"{APP}/originals/{original.pk}",
        f"{APP}/cases/{original.pk}",
        f"{APP}/cases/not-an-id",
        f"{APP}/originals/{public_id_of(original).upper()}",
    ]


@pytest.mark.usefixtures("finding_a")
def test_production_answers_byte_identical_to_development(
    settings: Settings, owner_a: User, original_a: Reference, direct_fetch: list[int]
) -> None:
    browser = client_for(owner_a)
    on_dev = {path: browser.get(path) for path in customer_paths(original_a)}

    settings.MARCELINE_ROLE = "prod"
    on_prod = {path: browser.get(path) for path in customer_paths(original_a)}

    for path, dev_response in on_dev.items():
        prod_response = on_prod[path]
        assert prod_response.status_code == dev_response.status_code, path
        assert prod_response.content == dev_response.content, path
        assert prod_response["Content-Type"] == dev_response["Content-Type"], path
        assert "X-Data-As-Of" in prod_response.headers, path
        assert "X-Data-As-Of" not in dev_response.headers, path
    assert direct_fetch == [owner_a.memberships.get().organisation_id], "one fetch, every screen"
    assert b"Alpha's own original" in on_prod[f"{APP}/originals"].content


@pytest.mark.usefixtures("finding_a")
def test_tenancy_survives_the_cache(
    settings: Settings,
    owner_a: User,
    owner_b: User,
    original_a: Reference,
    finding_a: Page,
    direct_fetch: list[int],
) -> None:
    settings.MARCELINE_ROLE = "prod"
    bravo = client_for(owner_b)

    foreign = bravo.get(f"{APP}/originals/{public_id_of(original_a)}")
    missing = bravo.get(f"{APP}/originals/{UNKNOWN_ORIGINAL}")
    foreign_case = bravo.get(f"{APP}/cases/{public_id_of(original_a)}")

    assert foreign.status_code == missing.status_code == foreign_case.status_code == 404
    assert foreign.content == missing.content
    assert bravo.get(f"{APP}/originals").json() == []
    assert b"alpha" not in bravo.get(f"{APP}/findings").content.lower()
    bravo_bundle = CachedBundle.objects.get(
        organisation_id=owner_b.memberships.get().organisation_id
    )
    alpha_bundle = CachedBundle.objects.filter(
        organisation_id=owner_a.memberships.get().organisation_id
    )
    assert finding_a.pk not in bravo_bundle.page_ids
    assert not alpha_bundle.exists(), "Bravo's requests never fetched Alpha's bundle"


@pytest.mark.usefixtures("finding_a")
def test_the_bundle_lists_its_pages_and_omits_flagged_ones(
    keys: KeyPair,
    alpha: Organisation,
    finding_a: Page,
    make_page: Callable[[str], Page],
    original_a: Reference,
) -> None:
    flagged = make_page("flagged-leak")
    flagged.safety_status = Page.SafetyStatus.BLOCKED
    flagged.save(update_fields=["safety_status"])
    ReferenceLeak.objects.create(reference=original_a, page=flagged, status="confirmed")

    payload = export.bundle_payload(alpha, now=timezone.now())

    assert payload is not None
    assert payload["page_ids"] == [finding_a.pk]
    assert set(payload["documents"]) == {
        "originals",
        "findings",
        "cases",
        "removals",
        "rights",
        "notices",
        # ADR 0055: the account-control status, one document per organisation like `rights`.
        "verification",
        # ADR 0056: the creator's own data requests, the same shape again.
        "data-requests",
        f"originals/{public_id_of(original_a)}",
        f"cases/{public_id_of(original_a)}",
        f"notices/candidates/{public_id_of(original_a)}",
        "originals/*",
        "cases/*",
        "notices/*",
        "notices/candidates/*",
    }
    assert "flagged-leak" not in json.dumps(payload["documents"])
    # The documents name the page by public id only; `page_ids` carries the key purges use.
    findings = json.loads(payload["documents"]["findings"]["body"])
    assert [row["page_id"] for row in findings] == [finding_a.public_id]
    assert [row["original_id"] for row in findings] == [public_id_of(original_a)]


@pytest.mark.usefixtures("finding_a")
def test_a_safety_purge_by_page_key_drops_the_bundle_that_names_the_page_by_public_id(
    settings: Settings,
    keys: KeyPair,
    owner_a: User,
    finding_a: Page,
    direct_fetch: list[int],
) -> None:
    browser = client_for(owner_a)
    settings.MARCELINE_ROLE = "prod"
    served = browser.get(f"{APP}/findings")
    assert [row["page_id"] for row in served.json()] == [finding_a.public_id]
    assert CachedBundle.objects.get().page_ids == [finding_a.pk]

    # Development blocks the page and purges it by primary key (`cache_purge.purge_flagged_page`).
    Page.objects.filter(pk=finding_a.pk).update(safety_status=Page.SafetyStatus.BLOCKED)
    now = timezone.now()
    wire = purge_wire(keys.private, page_ids=(finding_a.pk,), issued_at=now)
    result = apply_purge(verify_purge(wire, keys.private.public_key(), now=now), now=now)

    assert result.bundles == 1
    assert not CachedBundle.objects.exists()
    after = browser.get(f"{APP}/findings")
    assert (after.status_code, after.json()) == (200, [])
    assert direct_fetch == [owner_a.memberships.get().organisation_id] * 2


@pytest.mark.usefixtures("finding_a")
def test_an_unusable_signing_key_answers_the_exports_503_not_a_500(
    settings: Settings, alpha: Organisation, owner_a: User, export_token: str, tmp_path: Path
) -> None:
    """The export endpoint promises 503 `export_unavailable` for a key it cannot use.

    A missing key file raised `EnvelopeError`, which the endpoint turns into that 503; a file that
    exists and is not a PEM key (a truncated copy, a stray text file) raised `ValueError` instead
    and reached production as a 500 with no code to log.
    """
    settings.PUBLIC_SURFACE = "export"
    rubbish = tmp_path / "not-a-key"
    rubbish.write_text("-----BEGIN NOTHING-----\n")
    settings.CACHE_SIGNING_KEY_PATH = str(rubbish)

    response = Client().get(
        f"{EXPORT}/organisations/{alpha.pk}", HTTP_AUTHORIZATION=f"Bearer {export_token}"
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "export_unavailable"}


def test_an_organisation_without_members_exports_nothing(keys: KeyPair, db: None) -> None:
    empty = make_organisation("Nobody Here")

    assert export.signed_bundle(empty.pk, now=timezone.now()) is None
    assert export.signed_bundle(empty.pk + 999, now=timezone.now()) is None


@pytest.mark.django_db
def test_prod_answers_503_not_an_empty_list_when_nothing_can_be_served(
    settings: Settings, owner_a: User, keys: KeyPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MARCELINE_ROLE = "prod"

    def development_down(organisation_id: int) -> bytes:
        raise delivery_client.DeliveryError("connection_failed")

    monkeypatch.setattr(delivery_client, "fetch_bundle", development_down)

    response = client_for(owner_a).get(f"{APP}/originals")

    assert response.status_code == 503
    assert response.json() == {"detail": "data_temporarily_unavailable"}
    assert response["Retry-After"] == "60"


@pytest.mark.usefixtures("finding_a")
def test_prod_serves_stale_with_its_age_then_503_after_72_hours(
    settings: Settings, owner_a: User, direct_fetch: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MARCELINE_ROLE = "prod"
    browser = client_for(owner_a)
    fresh = browser.get(f"{APP}/originals")

    def development_down(organisation_id: int) -> bytes:
        raise delivery_client.DeliveryError("timeout")

    monkeypatch.setattr(delivery_client, "fetch_bundle", development_down)
    start = timezone.now()
    monkeypatch.setattr(customer_cache, "clock", lambda: start + timedelta(hours=48))
    stale = browser.get(f"{APP}/originals")
    monkeypatch.setattr(customer_cache, "clock", lambda: start + timedelta(hours=73))
    gone = browser.get(f"{APP}/originals")

    assert stale.status_code == 200
    assert stale.content == fresh.content
    assert stale["X-Data-As-Of"] == fresh["X-Data-As-Of"]
    assert gone.status_code == 503


@pytest.mark.django_db
def test_prod_passes_anonymous_and_organisation_less_callers_to_the_views(
    settings: Settings, keys: KeyPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MARCELINE_ROLE = "prod"
    monkeypatch.setattr(delivery_client, "fetch_bundle", lambda org: pytest.fail("must not fetch"))
    loner = User.objects.create_user("no-org", password="secret-pass-123")

    assert Client().get(f"{APP}/originals").status_code == 401
    assert client_for(loner).get(f"{APP}/originals").json() == []
    assert client_for(loner).get(f"{APP}/cases/abc").status_code == 404


# --- the export API over HTTP ------------------------------------------------------------------


@pytest.fixture
def export_token(db: None) -> str:
    return create_token("prod")[1]


@pytest.fixture
def export_surface(settings: Settings, keys: KeyPair) -> None:
    settings.PUBLIC_SURFACE = "export"


@pytest.mark.usefixtures("export_surface")
def test_the_export_api_needs_a_live_token(
    alpha: Organisation, owner_a: User, export_token: str
) -> None:
    path = f"{EXPORT}/organisations/{alpha.pk}"

    assert Client().get(path).status_code == 401
    assert Client().get(path, HTTP_AUTHORIZATION="Bearer mdx_wrong").status_code == 401
    ok = Client().get(path, HTTP_AUTHORIZATION=f"Bearer {export_token}")
    assert ok.status_code == 200
    ServiceToken.objects.update(revoked_at=timezone.now())
    assert Client().get(path, HTTP_AUTHORIZATION=f"Bearer {export_token}").status_code == 401


@pytest.mark.usefixtures("export_surface", "finding_a")
def test_production_fetches_over_http_and_verifies(
    settings: Settings,
    alpha: Organisation,
    export_token: str,
    keys: KeyPair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    django_client = Client()

    def development(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "dev-export.test"
        answer = django_client.get(
            request.url.path, HTTP_AUTHORIZATION=request.headers["Authorization"]
        )
        return httpx.Response(answer.status_code, content=answer.content)

    monkeypatch.setattr(delivery_client, "transport", httpx.MockTransport(development))
    settings.CACHE_EXPORT_BASE_URL = "http://dev-export.test:8010"
    settings.CACHE_EXPORT_TOKEN = export_token

    wire = delivery_client.fetch_bundle(alpha.pk)
    accounts = open_envelope(
        delivery_client.fetch_accounts(), keys.private.public_key(), expected_format=FORMAT_ACCOUNTS
    )

    assert json.loads(wire)["alg"] == "Ed25519"
    assert [user["username"] for user in accounts["users"]] == ["alpha-owner"]
    with pytest.raises(delivery_client.DeliveryError, match="not_found"):
        delivery_client.fetch_bundle(alpha.pk + 999)
    settings.CACHE_EXPORT_TOKEN = "mdx_wrong"
    with pytest.raises(delivery_client.DeliveryError, match="unauthorised"):
        delivery_client.fetch_bundle(alpha.pk)


@pytest.mark.usefixtures("keys")
@pytest.mark.parametrize("surface", ["full", "export", "customer", "internal"])
def test_production_never_serves_the_export_api(
    settings: Settings, alpha: Organisation, owner_a: User, export_token: str, surface: str
) -> None:
    settings.MARCELINE_ROLE = "prod"
    settings.PUBLIC_SURFACE = surface

    response = Client().get(
        f"{EXPORT}/organisations/{alpha.pk}", HTTP_AUTHORIZATION=f"Bearer {export_token}"
    )

    assert response.status_code == 404
    assert response.content == b""


# --- accounts replication ----------------------------------------------------------------------


def test_accounts_replicate_without_staff_flags_and_sign_in_works_on_prod(
    settings: Settings, keys: KeyPair, owner_a: User, owner_b: User
) -> None:
    owner_a.is_staff = owner_a.is_superuser = True
    owner_a.save()
    staff_only = User.objects.create_user("reviewer", password="secret-pass-123", is_staff=True)
    wire = export.signed_accounts(now=timezone.now())
    payload = verify_accounts(wire, keys.private.public_key(), now=timezone.now())

    assert {user.username for user in payload.users} == {"alpha-owner", "bravo-owner"}
    assert "is_staff" not in json.dumps(payload.model_dump(mode="json"))

    # Production's own tables, as a stale copy would have them: a stray user, an old role, and the
    # random public id production's own migration gave an organisation before the first pull.
    alpha = owner_a.memberships.get().organisation
    development_public_id = alpha.public_id
    Organisation.objects.filter(pk=alpha.pk).update(public_id="org_" + "e" * 32)
    Membership.objects.filter(user=owner_b).update(role=Membership.Role.MEMBER)
    settings.MARCELINE_ROLE = "prod"
    apply_accounts(payload, now=timezone.now())

    assert not User.objects.filter(pk=staff_only.pk).exists()
    replica = User.objects.get(pk=owner_a.pk)
    assert (replica.is_staff, replica.is_superuser) == (False, False)
    assert Membership.objects.get(user=owner_b).role == Membership.Role.OWNER
    browser = Client()
    browser.get(f"{APP}/auth/session")
    login = browser.post(
        f"{APP}/auth/login",
        {"username": "alpha-owner", "password": "secret-pass-123"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=browser.cookies["csrftoken"].value,
    )
    assert login.status_code == 200
    assert login.json()["organisation"]["name"] == "Alpha Studio"
    assert login.json()["organisation"]["id"] == development_public_id
    assert login.json()["monitored_sources"] == payload.monitored_sources


def test_an_older_accounts_document_is_refused(keys: KeyPair, owner_a: User) -> None:
    now = timezone.now()
    newer = verify_accounts(export.signed_accounts(now=now), keys.private.public_key(), now=now)
    older = verify_accounts(
        export.signed_accounts(now=now - timedelta(minutes=30)), keys.private.public_key(), now=now
    )
    apply_accounts(newer, now=now)

    with pytest.raises(Exception, match="as_of_went_backwards"):
        apply_accounts(older, now=now)


#: Customer reads production answers from its **own** tables on purpose: they are about the
#: session and the replicated accounts (ADR 0028 "Accounts, sessions and other writes"), or about
#: the local outbox (`fingerprints/submissions`), never about originals, findings or rights.
PRODUCTION_LOCAL_ROUTES = frozenset(
    {
        "api/v1/app/me",
        "api/v1/app/auth/session",
        "api/v1/app/fingerprints/submissions",
        "api/v1/app/notices/submissions",
        # The invite's state (ADR 0052 §9, slice 2): production's own invite rows, replicated by
        # `pull_accounts` with the accounts themselves. There is no organisation to key a bundle on
        # before the account exists, and an answer it does not have is the neutral 404 every failing
        # token gets — never an empty list that reads as data.
        "api/v1/app/invite/<token>",
        # The two mailed links (ADR 0054). Read from production's **own** replicated token rows,
        # like the invite's state and for the same reason: they belong to the host the reader is
        # typing into, there is no organisation to key a bundle on, and what production does not
        # have is the one neutral 404 every failing link gets — never an empty list that reads as
        # data.
        "api/v1/app/auth/password-reset/<token>",
        "api/v1/app/auth/verify-email/<token>",
    }
)
#: Customer reads whose **view** takes the document out of the signed bundle itself instead of
#: leaving it to the middleware (ADR 0046): the notice download, because serving it records an
#: `exported` event and a write belongs in a view. Still bundle data, never local tables.
CACHE_READING_ROUTES = frozenset({"api/v1/app/notices/<notice_id>/export/<fmt>"})


def _customer_get_routes() -> list[tuple[str, str]]:
    """(URL, Django route) for every customer GET operation, from the live schema."""
    schema = api.get_openapi_schema()
    return [
        (path, path.lstrip("/").replace("{", "<").replace("}", ">"))
        for path, methods in schema["paths"].items()
        if path.startswith(APP + "/") and "get" in methods
    ]


def test_every_customer_read_is_cached_production_local_or_development_only() -> None:
    """The third wall of ADR 0028: nothing may be answered from production's empty tables.

    Production's database holds no originals, findings or rights records. A customer read that is
    neither served from the signed bundle (`CACHED_ROUTES`) nor deliberately production-local
    would answer `[]` there — "nothing found", the one answer ADR 0028 forbids, and it would look
    like data rather than a missing wire. The rule is written down in delivery/CLAUDE.md; this is
    the test that notices when a new endpoint forgets it.
    """
    routes = _customer_get_routes()
    assert ("/api/v1/app/cases", "api/v1/app/cases") in routes, "the walk found no customer reads"

    for path, route in routes:
        if (
            route in PRODUCTION_LOCAL_ROUTES
            or route in CACHE_READING_ROUTES
            or route in customer_cache.CACHED_ROUTES
        ):
            continue
        assert not role_allows("prod", path), (
            f"{path} is answered on production from its empty local tables. Either add it to "
            "export.document_keys, delivery.store.DOCUMENT_KEY and "
            "customer_cache.CACHED_ROUTES together, or keep it development-only in "
            "public_surface.ROLE_ONLY_PREFIXES."
        )


def test_every_cached_route_names_a_document_the_export_writes() -> None:
    """The two halves of a cached route must agree: the key production looks up is the key
    development signs, and `DOCUMENT_KEY` must accept it."""
    for family, parameter in customer_cache.CACHED_ROUTES.values():
        if parameter is None:
            assert family in export.LIST_DOCUMENTS, family
            assert store.DOCUMENT_KEY.match(family), family
            continue
        assert family in export.DETAIL_FAMILIES, family
        missing = export.MISSING_BY_FAMILY[family]
        assert customer_cache.ID_PREFIXES[parameter] == missing.split("_", 1)[0], family
        for key in (f"{family}/{missing}", f"{family}/*"):
            assert store.DOCUMENT_KEY.match(key), key


def test_every_notice_download_key_is_one_the_store_accepts() -> None:
    """The download keys the export writes and the view looks up are the same shape (ADR 0046)."""
    notice_id = notice_services.unknown_notice_public_id()

    for fmt in notice_export.FORMATS:
        assert store.DOCUMENT_KEY.match(f"notices/{notice_id}/export/{fmt}"), fmt
    assert set(notice_export.FORMATS) == set(store.NOTICE_EXPORT_FORMATS)
    assert not store.DOCUMENT_KEY.match(f"notices/{notice_id}/export/pdf")


@pytest.fixture(autouse=True)
def _no_real_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network in tests")

    monkeypatch.setattr(delivery_client, "transport", httpx.MockTransport(refuse))
    yield
