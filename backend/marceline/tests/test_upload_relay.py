"""The streaming relay: the public host forwards a customer's file and keeps none of it.

Issue #59, ADR 0044 §9 and its amendment of 2026-09-19. The operator's decision is that cloud
fingerprinting must be available in the private beta on the public host, because independent
creators have no machine that can hash a 4K master. What makes that acceptable is not a promise but
this file: **every one of ADR 0044's no-retention guarantees is asserted here for the public host**,
where until now the answer was simply "the route is 404".

What each layer does with a large body, so the claim "nothing is written" can be read rather than
believed:

- **Caddy** limits the body (`request_body max_size`, exactly the two chunk paths) and *streams* it;
  buffering would need `request_buffers`, which the Caddyfile never sets —
  `test_prod_edge_config.test_exactly_the_two_chunk_paths_carry_a_chunk` asserts both.
- **gunicorn** hands the application a socket-backed `wsgi.input`. It has no request-body spooling
  of any kind: nothing in gunicorn writes a request body to disk.
- **Django** writes an upload to `FILE_UPLOAD_TEMP_DIR` in exactly one place: the multipart parser's
  `TemporaryFileUploadHandler`, reached through `request.POST` / `request.FILES`. The relay touches
  neither, and `DATA_UPLOAD_MAX_MEMORY_SIZE` never applies because `request.body` is never read —
  which is asserted below by a temporary directory that stays empty and by `request._body` staying
  unset.
- **The relay** reads `READ_BYTES` (64 KiB) at a time and yields each piece into the outgoing body,
  so the customer's bytes are in memory for one socket read and never longer.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.contrib.auth.models import User
from django.db import DatabaseError, IntegrityError, models, transaction
from django.test import Client
from pytest_django.fixtures import Settings

from core import public_ids
from core.deploy import CloudUploadSettings, cloud_upload_problems
from delivery.models import OutboxItem, ServiceToken
from delivery.tokens import create_token
from marceline.app.rights import CURRENT_VERSION
from marceline.app.upload_declaration import CURRENT_VERSION as DECLARATION_VERSION
from marceline.models import MediaUpload
from marceline.public_surface import (
    RELAY_PREFIX,
    dev_only_prefixes,
    role_allows,
    surface_allows,
)
from marceline.tests.app_helpers import APP, add_member, client_for, make_organisation
from marceline.uploads import attestation, relay
from marceline.uploads.models import RelayRefusal

Reason = RelayRefusal.Reason

pytestmark = pytest.mark.django_db

URL = f"{APP}/uploads"
#: The attestation headers, lower-cased as Django's test client takes them.
ORGANISATION = relay.ORGANISATION_HEADER.lower()
ACTOR = relay.USER_HEADER.lower()
RELAY_URL = f"{RELAY_PREFIX.rstrip('/')}/uploads"
CHUNK = 4096
UNKNOWN_UPLOAD = public_ids.generate("upl")


# --- configuration: which shapes may exist at all ----------------------------------------------


def _config(**overrides: Any) -> CloudUploadSettings:
    values: dict[str, Any] = {
        "role": "prod",
        "public_surface": "customer",
        "enabled": True,
        "relay_mode": True,
        "spool_dir": "",
        "relay_base_url": "http://100.64.0.1:8010",
        "relay_token": "mdx_relay",
    }
    values.update(overrides)
    return CloudUploadSettings(**values)


def test_the_public_host_may_hold_the_flag_only_in_relay_mode() -> None:
    """The invariant that replaced "the flag is false on production" (operator, 2026-09-19)."""
    assert cloud_upload_problems(_config()) == []
    assert "CLOUD_UPLOAD_RELAY_MODE" in " ".join(cloud_upload_problems(_config(relay_mode=False)))


def test_a_relay_never_has_a_spool_and_development_never_relays() -> None:
    with_spool = cloud_upload_problems(_config(spool_dir="/var/lib/marceline/upload-spool"))
    assert any("no spool" in problem for problem in with_spool)
    on_development = cloud_upload_problems(_config(role="dev"))
    assert any("point at itself" in problem for problem in on_development)
    internal = cloud_upload_problems(_config(public_surface="internal"))
    assert any("internal listener" in problem for problem in internal)


def test_a_relay_without_an_address_or_a_credential_is_refused() -> None:
    assert cloud_upload_problems(_config(relay_base_url=""))
    assert cloud_upload_problems(_config(relay_token=""))
    # Configured but switched off is fine: the routes are 404 either way.
    assert cloud_upload_problems(_config(enabled=False, relay_base_url="", relay_token="")) == []


def test_the_upload_prefix_leaves_the_development_only_list_only_in_relay_mode() -> None:
    assert URL in dev_only_prefixes(relay_mode=False)
    assert URL not in dev_only_prefixes(relay_mode=True)
    assert role_allows("prod", URL, relay_mode=False) is False
    assert role_allows("prod", URL, relay_mode=True) is True
    # Everything else that is development-only stays so, relay mode or not.
    for path in (f"{APP}/rights/proofs", f"{APP}/invoices", f"{APP}/notices/sandbox-send/x"):
        assert role_allows("prod", path, relay_mode=True) is False


def test_developments_relay_listener_is_reachable_only_where_it_should_be() -> None:
    path = f"{RELAY_URL}/{UNKNOWN_UPLOAD}"
    assert surface_allows("export", path) is True
    assert surface_allows("customer", path) is False
    assert surface_allows("internal", path) is False
    # The receiver is development's. On the public host the path does not exist at all.
    assert role_allows("prod", path, relay_mode=True) is False
    assert role_allows("dev", path) is True


# --- the fixtures for a real relayed request ----------------------------------------------------


@pytest.fixture
def relay_token() -> str:
    _row, token = create_token("relay-test", ServiceToken.Scope.UPLOAD_RELAY)
    return token


@pytest.fixture
def export_token() -> str:
    _row, token = create_token("export-test", ServiceToken.Scope.EXPORT)
    return token


@pytest.fixture
def spooled(tmp_path: Path, settings: Settings) -> Path:
    """Development's spool, and a temporary directory Django would write an upload into if it ever
    did. Both are checked after a relayed chunk: the first must stay empty on the public host's
    side, the second must stay empty on both."""
    settings.CLOUD_FINGERPRINTING_ENABLED = True
    settings.CLOUD_UPLOAD_SPOOL_DIR = str(tmp_path / "spool")
    settings.CLOUD_UPLOAD_CHUNK_BYTES = CHUNK
    settings.CLOUD_UPLOAD_MAX_BYTES = 100 * CHUNK
    settings.CLOUD_UPLOAD_SPOOL_MAX_BYTES = 1000 * CHUNK
    settings.CLOUD_UPLOAD_DAILY_BYTES_PER_ORG = 500 * CHUNK
    upload_temp = tmp_path / "django-upload-temp"
    upload_temp.mkdir()
    settings.FILE_UPLOAD_TEMP_DIR = str(upload_temp)
    return tmp_path / "spool"


@pytest.fixture
def customer(spooled: Path) -> Client:
    return client_for(add_member(make_organisation("Studio Relay"), "customer-relay"))


@pytest.fixture
def development(
    relay_token: str, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> list[httpx.Request]:
    """Development, as production reaches it: one `httpx.MockTransport` in front of a Django test
    client that answers `/internal/v1/relay/…`. Hands back the requests that crossed the wire.

    One process cannot be both machines, so the handler flips `MARCELINE_ROLE` for the duration of
    development's answer. That is a test artefact and nothing else — the relay endpoints refuse a
    `prod` role (`_relay_ready`), which is asserted separately above and below.
    """
    django_client = Client()
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "dev-relay.test", request.url.host
        body = request.read()
        seen.append(request)
        role = settings.MARCELINE_ROLE
        surface = settings.PUBLIC_SURFACE
        settings.MARCELINE_ROLE = "dev"
        settings.PUBLIC_SURFACE = "export"
        try:
            response = django_client.generic(
                request.method,
                request.url.path,
                data=body,
                content_type=request.headers.get("Content-Type", "application/json"),
                headers={
                    "authorization": request.headers.get("Authorization", ""),
                    ORGANISATION: request.headers.get(relay.ORGANISATION_HEADER, ""),
                    ACTOR: request.headers.get(relay.USER_HEADER, ""),
                },
            )
        finally:
            settings.MARCELINE_ROLE = role
            settings.PUBLIC_SURFACE = surface
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"Content-Type": response.headers.get("Content-Type", "application/json")},
        )

    monkeypatch.setattr(relay, "transport", httpx.MockTransport(answer))
    settings.CLOUD_RELAY_BASE_URL = "http://dev-relay.test:8010"
    settings.CLOUD_RELAY_TOKEN = relay_token
    return seen


@pytest.fixture
def public_host(settings: Settings) -> None:
    settings.MARCELINE_ROLE = "prod"
    settings.PUBLIC_SURFACE = "customer"
    settings.CLOUD_UPLOAD_RELAY_MODE = True


def _start(client: Client, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "file_size": 2 * CHUNK,
        "title": "An original",
        "creators": ["A Creator"],
        "fps": 1.0,
        "rights_confirmed": True,
        "rights_text_version": CURRENT_VERSION,
        "language": "en",
        "sole_performer_confirmed": True,
        "article9_consent": True,
        "upload_declaration_version": DECLARATION_VERSION,
    }
    payload.update(overrides)
    return client.post(URL, payload, content_type="application/json")


def _relay_payload(**overrides: Any) -> str:
    """A declare body as the public host forwards it, for the tests that call development direct."""
    payload: dict[str, Any] = {
        "file_size": CHUNK,
        "title": "An original",
        "creators": ["A Creator"],
        "fps": 1.0,
        "rights_confirmed": True,
        "rights_text_version": CURRENT_VERSION,
        "language": "en",
        "sole_performer_confirmed": True,
        "article9_consent": True,
        "upload_declaration_version": DECLARATION_VERSION,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _files_under(directory: Path) -> list[str]:
    return sorted(
        str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_file()
    )


# --- the relayed upload, end to end -------------------------------------------------------------


@pytest.mark.usefixtures("public_host")
def test_an_upload_crosses_the_public_host_and_is_written_only_on_development(
    customer: Client, development: list[httpx.Request], spooled: Path, tmp_path: Path, caplog: Any
) -> None:
    """The whole point, in one test: a customer on `dash.marceline.studio` sends a file, the bytes
    land in development's spool, and the public host holds nothing at any moment."""
    caplog.set_level(logging.INFO)
    started = _start(customer)
    assert started.status_code == 201, started.content
    upload = started.json()
    assert upload["chunk_bytes"] == CHUNK and upload["next_chunk"] == 0

    data = bytes(range(256)) * (2 * CHUNK // 256)
    for index in range(2):
        chunk = data[index * CHUNK : (index + 1) * CHUNK]
        answer = customer.post(
            f"{URL}/{upload['id']}/chunks/{index}",
            data=chunk,
            content_type="application/octet-stream",
        )
        assert answer.status_code == 200, answer.content
        assert answer.json()["received_bytes"] == (index + 1) * CHUNK

    completed = customer.post(
        f"{URL}/{upload['id']}/complete",
        {"sha256": hashlib.sha256(data).hexdigest()},
        content_type="application/json",
    )
    assert completed.status_code == 200, completed.content
    assert completed.json()["status"] == "queued"

    # Development holds the row and the bytes; it is the only machine that does.
    job = MediaUpload.objects.get()
    assert job.origin == MediaUpload.Origin.RELAY
    assert job.status == MediaUpload.Status.QUEUED
    assert job.received_bytes == 2 * CHUNK
    assert (spooled / job.public_id / "upload").read_bytes() == data

    # (b) never in Postgres on the public host: the relay issued no write of its own, and a relayed
    # request is not an outbox item — it is answered by the machine that owns the data.
    assert OutboxItem.objects.count() == 0
    # Django's only path to a temporary file for an upload is the multipart parser; the relay never
    # reaches it, so this directory is untouched.
    assert _files_under(tmp_path / "django-upload-temp") == []
    # (c) nothing in a log names the bytes, the spool or the credential.
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert str(spooled) not in logged
    assert "mdx_" not in logged
    assert data[:32].hex() not in logged


@pytest.mark.usefixtures("public_host")
def test_the_relayed_status_read_is_the_row_development_holds(
    customer: Client, development: list[httpx.Request], spooled: Path
) -> None:
    """Progress comes back the way every other read does — as the answer of the machine that has
    it. There is nothing for a signed bundle to carry: the row is created and deleted within
    minutes, and a progress bar cannot be eventually consistent."""
    upload = _start(customer).json()
    MediaUpload.objects.filter(public_id=upload["id"]).update(
        status=MediaUpload.Status.HASHING, frames_done=7, frames_total=30
    )

    one = customer.get(f"{URL}/{upload['id']}")
    listed = customer.get(URL)

    assert one.status_code == 200
    assert one.json()["frames_done"] == 7 and one.json()["frames_total"] == 30
    assert one.json()["status"] == "hashing"
    assert [row["id"] for row in listed.json()] == [upload["id"]]
    # The answer is the customer API's own schema, byte for byte — the relay passes it through.
    assert set(one.json()) == {
        "id",
        "title",
        "status",
        "code",
        "reason",
        "original_id",
        "declared_bytes",
        "received_bytes",
        "chunk_bytes",
        "next_chunk",
        "frames_done",
        "frames_total",
        "bytes_deleted",
        "created_at",
    }


@pytest.mark.usefixtures("public_host")
def test_a_cancel_crosses_the_relay_and_the_bytes_go_at_once(
    customer: Client, development: list[httpx.Request], spooled: Path
) -> None:
    upload = _start(customer).json()
    customer.post(
        f"{URL}/{upload['id']}/chunks/0",
        data=b"x" * CHUNK,
        content_type="application/octet-stream",
    )

    cancelled = customer.delete(f"{URL}/{upload['id']}")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["bytes_deleted"] is True
    assert not (spooled / upload["id"]).exists()


# --- the wire: what leaves the public host ------------------------------------------------------


@pytest.mark.usefixtures("public_host")
def test_the_chunk_leaves_with_a_length_a_scope_and_an_attestation(
    customer: Client,
    development: list[httpx.Request],
    spooled: Path,
    relay_token: str,
) -> None:
    """The outgoing request is a plain body with a `Content-Length` — not `Transfer-Encoding:
    chunked`, which httpx would use for a body generator with no declared length, and which
    development would then have to read differently from a browser's."""
    upload = _start(customer).json()
    development.clear()

    customer.post(
        f"{URL}/{upload['id']}/chunks/0",
        data=b"y" * CHUNK,
        content_type="application/octet-stream",
    )

    [chunk_request] = development
    assert chunk_request.headers["Content-Length"] == str(CHUNK)
    assert "Transfer-Encoding" not in chunk_request.headers
    assert chunk_request.headers["Authorization"] == f"Bearer {relay_token}"
    assert chunk_request.headers[relay.ORGANISATION_HEADER].isdigit()
    assert chunk_request.headers[relay.USER_HEADER].isdigit()
    assert chunk_request.url.path == f"{RELAY_URL}/{upload['id']}/chunks/0"
    # The file name never reached this machine, so there is nothing about it to forward.
    assert "filename" not in str(chunk_request.headers).lower()


@pytest.mark.usefixtures("public_host")
def test_a_chunk_relays_although_django_could_not_hold_it_and_could_not_write_it(
    customer: Client,
    development: list[httpx.Request],
    spooled: Path,
    tmp_path: Path,
    settings: Settings,
) -> None:
    """The sharpest proof available that the body is neither buffered nor spooled here.

    `DATA_UPLOAD_MAX_MEMORY_SIZE` is set **below** the chunk size: anything that read
    `request.body` would raise `RequestDataTooBig` and answer 400. And `FILE_UPLOAD_TEMP_DIR` is
    made read-only: anything that engaged Django's multipart `TemporaryFileUploadHandler` — the one
    place in Django that writes an upload to disk — would fail with `PermissionError`. The request
    succeeds, so neither happened.
    """
    upload = _start(customer).json()
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 512
    temporary = tmp_path / "django-upload-temp"
    temporary.chmod(0o500)
    try:
        answer = customer.post(
            f"{URL}/{upload['id']}/chunks/0",
            data=b"q" * CHUNK,
            content_type="application/octet-stream",
        )
    finally:
        temporary.chmod(0o700)

    assert answer.status_code == 200, answer.content
    assert answer.json()["received_bytes"] == CHUNK
    assert _files_under(temporary) == []


def test_the_relay_module_issues_no_query_and_holds_no_model(settings: Settings) -> None:
    """(b) never in Postgres, on the public host, read straight off the module: the relay's code
    mentions no model and no manager, so there is no write it could be doing."""
    source = Path(relay.__file__).read_text("utf-8")
    tree = ast.parse(source)
    code = ast.unparse(ast.Module(body=tree.body[1:], type_ignores=[]))

    assert "objects." not in code
    assert "models" not in code
    assert "MediaUpload" not in code
    # And nothing writes a file: no open(), no Path, no tempfile, no NamedTemporaryFile.
    for forbidden in ("open(", "tempfile", "NamedTemporary", "Path(", "os.write"):
        assert forbidden not in code, forbidden


@pytest.mark.usefixtures("public_host")
@pytest.mark.parametrize(
    "content_type", ["multipart/form-data; boundary=x", "application/x-www-form-urlencoded"]
)
def test_a_chunk_that_claims_to_be_a_form_is_refused_rather_than_relayed_empty(
    customer: Client, development: list[httpx.Request], spooled: Path, content_type: str
) -> None:
    """Django's CSRF check reads `request.POST` for every POST. For `application/octet-stream` that
    is an empty `QueryDict` and the body is untouched (`HttpRequest._load_post_and_files`), but a
    form content type makes it **parse the body** — and the multipart branch is the one that writes
    a temporary file. So a chunk that claims to be a form is refused before anything is read."""
    upload = _start(customer).json()
    development.clear()

    answer = customer.post(
        f"{URL}/{upload['id']}/chunks/0", data=b"y" * CHUNK, content_type=content_type
    )

    assert answer.status_code == 400
    assert answer.json()["detail"] == "invalid_request"
    assert development == [], "nothing may be forwarded"


@pytest.mark.usefixtures("public_host")
def test_a_customer_disconnecting_mid_chunk_is_not_a_server_error(
    customer: Client,
    development: list[httpx.Request],
    spooled: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser that goes away while a chunk is in flight makes Django's own read raise
    (`UnreadablePostError`, an `OSError`). That is not a 500 and not a Sentry event: it is the same
    503 code as any other relay problem, and nothing of the half-sent body is anywhere."""
    upload = _start(customer).json()
    development.clear()

    def collapse(request: httpx.Request) -> httpx.Response:
        raise OSError("request data read error")

    settings.CLOUD_RELAY_BASE_URL = "http://dev-relay.test:8010"
    monkeypatch.setattr(relay, "transport", httpx.MockTransport(collapse))

    answer = customer.post(
        f"{URL}/{upload['id']}/chunks/0",
        data=b"z" * CHUNK,
        content_type="application/octet-stream",
    )

    assert answer.status_code == 503
    assert answer.json()["detail"] == "uploads_unavailable"


@pytest.mark.usefixtures("public_host")
def test_an_answer_too_large_to_be_a_status_is_refused_not_truncated(
    customer: Client, spooled: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing this API answers is large. Handing the browser half a JSON document would be worse
    than handing it a code, so an oversized answer becomes the same 503 as any relay problem."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (relay.MAX_ANSWER_BYTES + 1))

    monkeypatch.setattr(relay, "transport", httpx.MockTransport(answer))
    settings.CLOUD_RELAY_BASE_URL = "http://dev-relay.test:8010"
    settings.CLOUD_RELAY_TOKEN = "mdx_relay"

    response = customer.get(URL)

    assert response.status_code == 503
    assert response.json()["detail"] == "uploads_unavailable"


def test_the_body_is_read_in_bounded_pieces(settings: Settings) -> None:
    """`relay._pieces` is what keeps the memory claim true, so it is asserted directly: every read
    is at most `READ_BYTES`, whatever the chunk size, and the body is never materialised."""

    class Recording:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self.reads: list[int] = []

        def read(self, size: int) -> bytes:
            self.reads.append(size)
            piece, self._data = self._data[:size], self._data[size:]
            return piece

    body = b"z" * (3 * relay.READ_BYTES + 17)
    recording = Recording(body)

    pieces = list(relay._pieces(recording, len(body)))  # type: ignore[arg-type]

    assert b"".join(pieces) == body
    assert max(recording.reads) <= relay.READ_BYTES
    assert max(len(piece) for piece in pieces) <= relay.READ_BYTES


def test_a_short_read_ends_the_body_rather_than_looping(settings: Settings) -> None:
    class Truncated:
        def read(self, size: int) -> bytes:
            return b""

    assert list(relay._pieces(Truncated(), 1024)) == []  # type: ignore[arg-type]


# --- failures: a relay problem is never a customer-facing surprise ------------------------------


@pytest.mark.usefixtures("public_host")
def test_a_relay_that_cannot_reach_development_answers_one_code(
    customer: Client, spooled: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(relay, "transport", httpx.MockTransport(refuse))
    settings.CLOUD_RELAY_BASE_URL = "http://dev-relay.test:8010"
    settings.CLOUD_RELAY_TOKEN = "mdx_relay"

    response = _start(customer)

    assert response.status_code == 503
    assert response.json()["detail"] == "uploads_unavailable"
    assert MediaUpload.objects.count() == 0


@pytest.mark.usefixtures("public_host")
@pytest.mark.parametrize("status", [401, 500, 502])
def test_an_answer_the_upload_api_does_not_document_becomes_one_503(
    customer: Client,
    spooled: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """A 401 from development must not reach the browser: the app would read it as "you are signed
    out" and throw the customer back to the login while their session is perfectly good."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "nope"})

    monkeypatch.setattr(relay, "transport", httpx.MockTransport(answer))
    settings.CLOUD_RELAY_BASE_URL = "http://dev-relay.test:8010"
    settings.CLOUD_RELAY_TOKEN = "mdx_relay"

    response = _start(customer)

    assert response.status_code == 503
    assert response.json()["detail"] == "uploads_unavailable"


@pytest.mark.usefixtures("public_host")
def test_the_feature_is_invisible_on_the_public_host_without_the_relay(
    customer: Client, settings: Settings
) -> None:
    """Every way of half-configuring it is off, and off means the bare 404 of an unknown path."""
    settings.CLOUD_UPLOAD_RELAY_MODE = False
    assert _start(customer).status_code == 404
    assert customer.get(URL).status_code == 404

    settings.CLOUD_UPLOAD_RELAY_MODE = True
    for base, token in (("", "mdx_relay"), ("http://dev-relay.test", ""), ("", "")):
        settings.CLOUD_RELAY_BASE_URL = base
        settings.CLOUD_RELAY_TOKEN = token
        assert _start(customer).status_code == 404
        assert customer.get(f"{APP}/me").json()["can_upload_media"] is False


# --- development's side: the credential and what it may touch -----------------------------------


def test_the_relay_listener_refuses_the_export_token(
    export_token: str, relay_token: str, spooled: Path
) -> None:
    """Two scopes, so neither credential can do the other's work (ADR 0028's export token is
    read-only, and this one may write an upload it created)."""
    path = f"{RELAY_URL}/{UNKNOWN_UPLOAD}"
    assert Client().get(path).status_code == 401
    assert (
        Client().get(path, headers={"authorization": f"Bearer {export_token}"}).status_code == 401
    )
    assert Client().get(path, headers={"authorization": "Bearer mdx_wrong"}).status_code == 401
    # The relay token resolves, and then the request is refused for its own reason: there is no
    # such upload, and no organisation was attested.
    assert Client().get(path, headers={"authorization": f"Bearer {relay_token}"}).status_code == 404


def test_the_relay_listener_is_off_with_the_feature(relay_token: str, settings: Settings) -> None:
    settings.CLOUD_FINGERPRINTING_ENABLED = False
    response = Client().get(
        f"{RELAY_URL}/{UNKNOWN_UPLOAD}", headers={"authorization": f"Bearer {relay_token}"}
    )
    assert response.status_code == 404
    assert response.content == b""


def test_an_unattested_or_mismatched_actor_is_the_same_404(relay_token: str, spooled: Path) -> None:
    """Every attestation that is not a real, active customer of ours gets **one** answer, byte for
    byte — otherwise the relay's caller has an oracle for which ids exist (ADR 0044 §9, amendment
    2026-09-20). The reason is counted on this machine instead of being sent."""
    organisation = make_organisation("Studio A")
    user = add_member(organisation, "customer-a")
    other = User.objects.create_user("stranger", password="secret-pass-123")
    switched_off = add_member(make_organisation("Studio Off"), "customer-off")
    User.objects.filter(pk=switched_off.pk).update(is_active=False)
    cases = {
        Reason.MALFORMED: ({}, {ORGANISATION: "not-a-number", ACTOR: "1"}),
        Reason.NO_MEMBERSHIP: (
            {ORGANISATION: str(organisation.pk), ACTOR: str(other.pk)},
            {ORGANISATION: str(organisation.pk + 999), ACTOR: str(user.pk)},
        ),
        Reason.INACTIVE: (
            {
                ORGANISATION: str(switched_off.memberships.get().organisation_id),
                ACTOR: str(switched_off.pk),
            },
        ),
    }
    answers = []
    for reason, attestations in cases.items():
        for attested in attestations:
            response = Client().post(
                RELAY_URL,
                data=_relay_payload(),
                content_type="application/json",
                headers={"authorization": f"Bearer {relay_token}", **attested},
            )
            assert response.status_code == 404, attested
            answers.append(response.content)
        assert RelayRefusal.objects.get(reason=reason).attempts == len(attestations)

    assert len(set(answers)) == 1, "every reason answers the same bytes"
    assert MediaUpload.objects.count() == 0


def test_uploads_that_are_not_available_here_are_the_same_404(
    relay_token: str, spooled: Path, settings: Settings
) -> None:
    """The relay is held to the gate a browser on this machine is held to (`_customer`): with no
    usable spool there is nothing to accept, and the answer is the route's own 404."""
    organisation = make_organisation("Studio A")
    user = add_member(organisation, "customer-a")
    settings.CLOUD_UPLOAD_SPOOL_DIR = ""

    response = Client().post(
        RELAY_URL,
        data=_relay_payload(),
        content_type="application/json",
        headers={
            "authorization": f"Bearer {relay_token}",
            ORGANISATION: str(organisation.pk),
            ACTOR: str(user.pk),
        },
    )

    assert response.status_code == 404
    assert RelayRefusal.objects.get().reason == Reason.UNAVAILABLE
    assert MediaUpload.objects.count() == 0


def test_the_relay_may_not_spread_over_more_organisations_than_the_ceiling(
    relay_token: str, spooled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One credential may not quietly serve the whole customer base (ADR 0044 §9, amendment
    2026-09-20). Verifying the pair cannot stop a compromised `web` naming a real customer; this
    bounds how many real customers it can name before it is refused and recorded."""
    monkeypatch.setattr(attestation, "MAX_ORGANISATIONS", 1)
    first = make_organisation("Studio A")
    first_user = add_member(first, "customer-a")
    second = make_organisation("Studio B")
    second_user = add_member(second, "customer-b")

    def start(organisation: Any, user: User) -> Any:
        return Client().post(
            RELAY_URL,
            data=_relay_payload(),
            content_type="application/json",
            headers={
                "authorization": f"Bearer {relay_token}",
                ORGANISATION: str(organisation.pk),
                ACTOR: str(user.pk),
            },
        )

    assert start(first, first_user).status_code == 201
    # The organisation already inside the window is unaffected, however many uploads it starts.
    assert start(first, first_user).status_code == 201

    refused = start(second, second_user)

    assert refused.status_code == 503
    assert refused.json()["detail"] == "uploads_unavailable"
    assert RelayRefusal.objects.get().reason == Reason.SPREAD
    assert not MediaUpload.objects.filter(organisation=second).exists()


@pytest.mark.parametrize(
    "missing", ["sole_performer_confirmed", "article9_consent", "upload_declaration_version"]
)
def test_a_relayed_upload_needs_the_declaration_exactly_as_a_local_one_does(
    relay_token: str, spooled: Path, missing: str
) -> None:
    """The relay calls `uploads.begin`, so the Art. 9 basis of the whole feature (ADR 0044
    amendment 2026-09-19, § B) is checked on this machine and not on the attesting one."""
    organisation = make_organisation("Studio A")
    user = add_member(organisation, "customer-a")

    response = Client().post(
        RELAY_URL,
        data=_relay_payload(**{missing: False if missing != "upload_declaration_version" else ""}),
        content_type="application/json",
        headers={
            "authorization": f"Bearer {relay_token}",
            ORGANISATION: str(organisation.pk),
            ACTOR: str(user.pk),
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "upload_declaration_required"
    assert MediaUpload.objects.count() == 0


def test_the_refusal_counter_names_nobody_and_cannot_be_rewritten(spooled: Path) -> None:
    """It is a counter, not a journal: a row that named the attested pair would grow without bound
    under exactly the caller it watches, and the pair is not what the alarm is about."""
    for field in RelayRefusal._meta.get_fields():
        assert not isinstance(field, models.ForeignKey), field.name
        assert field.name in ("id", "hour", "reason", "attempts", "first_at", "last_at")

    attestation.record(Reason.MALFORMED)
    attestation.record(Reason.MALFORMED)
    row = RelayRefusal.objects.get()
    assert row.attempts == 2

    with pytest.raises(IntegrityError), transaction.atomic():
        RelayRefusal.objects.filter(pk=row.pk).delete()
    with pytest.raises(IntegrityError), transaction.atomic():
        RelayRefusal.objects.filter(pk=row.pk).update(attempts=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        RelayRefusal.objects.filter(pk=row.pk).update(reason=Reason.SPREAD)


def test_an_audit_that_cannot_be_written_never_fails_the_request(
    monkeypatch: pytest.MonkeyPatch, spooled: Path, caplog: Any
) -> None:
    """The counter must not become a way to take the feature down."""

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise DatabaseError("no")

    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(RelayRefusal.objects, "get_or_create", broken)

    attestation.record(Reason.MALFORMED)

    assert "was not recorded" in caplog.text


def test_the_relay_only_ever_touches_an_upload_it_created(relay_token: str, spooled: Path) -> None:
    """ "An upload it created" (ADR 0044 §9) is a query, not a promise: a row the operator started
    over Tailscale is, to the relay, the same 404 as a stranger's.

    Reads are narrowed the same way since the amendment of 2026-09-20 — on the public host a
    customer's uploads are relay rows by construction, so the customer's own screen loses nothing,
    and a credential the internet can reach stops being a window onto rows it did not create.
    """
    organisation = make_organisation("Studio A")
    user = add_member(organisation, "customer-a")
    direct = MediaUpload.objects.create(
        organisation=organisation,
        created_by=user,
        origin=MediaUpload.Origin.DIRECT,
        declared_bytes=CHUNK,
        title="Hashed here",
        creators=["A Creator"],
        frame_fps=1.0,
        chunk_bytes=CHUNK,
        rights_text_version=CURRENT_VERSION,
        rights_language="en",
        upload_declaration_version=DECLARATION_VERSION,
        upload_declaration_language="en",
    )
    attest = {
        "authorization": f"Bearer {relay_token}",
        ORGANISATION: str(organisation.pk),
        ACTOR: str(user.pk),
    }

    chunk = Client().post(
        f"{RELAY_URL}/{direct.public_id}/chunks/0",
        data=b"x" * CHUNK,
        content_type="application/octet-stream",
        headers=attest,
    )
    complete = Client().post(
        f"{RELAY_URL}/{direct.public_id}/complete",
        data=json.dumps({"sha256": "ab" * 32}),
        content_type="application/json",
        headers=attest,
    )
    cancel = Client().delete(f"{RELAY_URL}/{direct.public_id}", headers=attest)
    read = Client().get(f"{RELAY_URL}/{direct.public_id}", headers=attest)
    listed = Client().get(RELAY_URL, headers=attest)

    assert chunk.status_code == 404
    assert complete.status_code == 404
    assert cancel.status_code == 404
    assert read.status_code == 404
    assert listed.status_code == 200 and listed.json() == []
    direct.refresh_from_db()
    assert direct.status == MediaUpload.Status.RECEIVING


def test_the_relay_listener_refuses_a_chunk_larger_than_the_cap(
    relay_token: str, spooled: Path
) -> None:
    organisation = make_organisation("Studio A")
    user = add_member(organisation, "customer-a")
    attest = {
        "authorization": f"Bearer {relay_token}",
        ORGANISATION: str(organisation.pk),
        ACTOR: str(user.pk),
    }
    started = Client().post(
        RELAY_URL,
        data=_relay_payload(file_size=2 * CHUNK),
        content_type="application/json",
        headers=attest,
    )
    assert started.status_code == 201, started.content

    too_large = Client().post(
        f"{RELAY_URL}/{started.json()['id']}/chunks/0",
        data=b"x" * (CHUNK * 4),
        content_type="application/octet-stream",
        headers=attest,
    )

    assert too_large.status_code == 413
