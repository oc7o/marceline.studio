"""The machine-to-machine API between development and production (`/internal/v1/…`, ADR 0028).

Deliberately a separate `NinjaAPI` with no docs and no OpenAPI document: it is not part of the
customer contract (`openapi.json`), and nothing about it should be discoverable.

- `GET /internal/v1/export/organisations/{id}` and `GET /internal/v1/export/accounts` —
  **development only** (`MARCELINE_ROLE=dev`), served by the `export` surface on development's
  Tailscale address, authenticated by a scoped service token, answering signed envelopes.
- `POST /internal/v1/cache/purge` — **production only** (`MARCELINE_ROLE=prod`), served by the
  `internal` surface behind a Caddy site bound to production's Tailscale address; the signature is
  the authentication.
- `/internal/v1/relay/uploads…` — **development only**, on the same `export` surface: the public
  host's streaming relay for cloud fingerprinting (ADR 0044 §9, amendment 2026-09-19). Its own token
  scope (`upload_relay`), so the read-only export token cannot reach it and this credential cannot
  read a bundle. 404 unless `CLOUD_FINGERPRINTING_ENABLED`.
- `POST /internal/v1/outbox/pull` and `POST /internal/v1/outbox/ack` — **production only**, on
  the same `internal` surface: development pulls customer writes (answered with a batch signed by
  production's own outbox key) and settles them; development's signature is the authentication
  (ADR 0028 amendment "The production write path"). 404 unless `OUTBOX_ENABLED`.

The role check here repeats `marceline/public_surface.py` on purpose: either one alone keeps an
export off production and a purge off development. Every refusal is terse and never says which
check failed; the reason goes to the log as a code.
"""

import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from ninja import NinjaAPI, Status
from ninja.security import HttpBearer

from accounts.models import Organisation
from delivery import outbox, purge, store, tokens
from delivery.envelope import EnvelopeError
from delivery.models import ServiceToken
from marceline import export
from marceline.app import uploads
from marceline.models import MediaUpload
from marceline.uploads import attestation, relay

logger = logging.getLogger("marceline.cache")

internal_api = NinjaAPI(
    title="Marceline internal API",
    version="1",
    urls_namespace="internal-v1",
    docs_url=None,
    openapi_url=None,
)


class ExportToken(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str) -> bool | None:
        return True if tokens.token_is_valid(token, ServiceToken.Scope.EXPORT) else None


class RelayToken(HttpBearer):
    """The upload relay's own credential (ADR 0044 §9). A different scope from the export token on
    purpose: neither can do the other's work, and revoking one leaves the other alone."""

    def authenticate(self, request: HttpRequest, token: str) -> bool | None:
        return True if tokens.token_is_valid(token, ServiceToken.Scope.UPLOAD_RELAY) else None


def _not_here() -> HttpResponse:
    return HttpResponse(status=404)


def _json_wire(wire: bytes) -> HttpResponse:
    return HttpResponse(wire, content_type="application/json")


@internal_api.get("/export/organisations/{organisation_id}", auth=ExportToken())
def export_organisation(request: HttpRequest, organisation_id: int) -> HttpResponse:
    """One organisation's signed bundle. 404 for an organisation with nothing to sign in to."""
    if settings.MARCELINE_ROLE != "dev":
        return _not_here()
    try:
        wire = export.signed_bundle(organisation_id, now=timezone.now())
    except EnvelopeError as exc:
        logger.error("export of organisation %s failed: %s", organisation_id, exc.code)
        return JsonResponse({"detail": "export_unavailable"}, status=503)
    if wire is None:
        return JsonResponse({"detail": "not_found"}, status=404)
    return _json_wire(wire)


@internal_api.get("/export/accounts", auth=ExportToken())
def export_accounts(request: HttpRequest) -> HttpResponse:
    """The signed accounts document production signs customers in with."""
    if settings.MARCELINE_ROLE != "dev":
        return _not_here()
    try:
        return _json_wire(export.signed_accounts(now=timezone.now()))
    except EnvelopeError as exc:
        logger.error("export of accounts failed: %s", exc.code)
        return JsonResponse({"detail": "export_unavailable"}, status=503)


# ---------------------------------------------------------------------------------------------
# The upload relay, development's side (ADR 0044 §9, amendment 2026-09-19). Six routes that mirror
# `/api/v1/app/uploads` and call **the same six functions** the customer routes call — the file
# lands here, where the spool, the worker and ffmpeg are, and the public host stores nothing.
#
# Production authenticates the customer's session and checks CSRF, then attests who is acting in two
# headers. Nothing here trusts anything else about the request: the credential's scope reaches only
# this prefix, every row it may touch — write **and read** — is one it created itself
# (`MediaUpload.origin`), and the organisation decides what may be seen, exactly as for a browser.
#
# The attestation itself is bound rather than believed (ADR 0044 §9, amendment 2026-09-20):
# `marceline/uploads/attestation.py` refuses every pair that is not a real, active, entitled
# customer, caps how many organisations one credential may spread over, and counts what it refused.
# ---------------------------------------------------------------------------------------------


def _relay_actor(request: HttpRequest) -> tuple[User, Organisation]:
    """The customer production says is acting, verified against development's own rows.

    Raises `uploads.Refused(404, "not_found")` for anything that does not resolve — one answer for
    every reason, because which of them applies is exactly what must not be learnable here.
    """
    return attestation.actor(
        request.headers.get(relay.ORGANISATION_HEADER),
        request.headers.get(relay.USER_HEADER),
    )


def _relay_ready() -> bool:
    """Whether this machine answers the relay at all: development, with the feature switched on."""
    return settings.MARCELINE_ROLE == "dev" and bool(settings.CLOUD_FINGERPRINTING_ENABLED)


def _relay_chunk_body(request: HttpRequest, limit: int) -> bytes:
    """One chunk, read past Django's global body cap and no further.

    `request.body` would raise on an 8 MiB body (`DATA_UPLOAD_MAX_MEMORY_SIZE`) and
    `allow_chunk_body` asks about a session this request does not have, so the read is spelled out
    here: a declared length, a cap, and a loop that stops at either.
    """
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise uploads.Refused(400, "invalid_request") from None
    if length <= 0 or length > limit:
        raise uploads.Refused(413, "too_large")
    pieces: list[bytes] = []
    read = 0
    while read < length:
        piece = request.read(min(relay.READ_BYTES, length - read))
        if not piece:
            break
        pieces.append(piece)
        read += len(piece)
    return b"".join(pieces)


@internal_api.post(
    "/relay/uploads",
    auth=RelayToken(),
    response={
        201: uploads.UploadOut,
        400: uploads.UploadErrorOut,
        403: uploads.UploadErrorOut,
        404: uploads.UploadErrorOut,
        429: uploads.UploadErrorOut,
        503: uploads.UploadErrorOut,
    },
)
def relay_start_upload(
    request: HttpRequest, payload: uploads.StartUploadIn, response: HttpResponse
) -> Status[uploads.UploadOut | uploads.UploadErrorOut] | HttpResponse:
    """Declare one upload on behalf of a customer the public host authenticated."""
    if not _relay_ready():
        return _not_here()
    try:
        user, organisation = _relay_actor(request)
        attestation.admit(organisation)
        job = uploads.begin(user, organisation, payload, origin=MediaUpload.Origin.RELAY)
        return Status(201, uploads.out(job))
    except uploads.Refused as refused:
        return uploads.refusal(response, refused)


@internal_api.post(
    "/relay/uploads/{upload_id}/chunks/{int:index}",
    auth=RelayToken(),
    response={
        200: uploads.UploadOut,
        400: uploads.UploadErrorOut,
        404: uploads.UploadErrorOut,
        409: uploads.UploadErrorOut,
        413: uploads.UploadErrorOut,
    },
)
def relay_put_chunk(
    request: HttpRequest, upload_id: str, index: int, response: HttpResponse
) -> Status[uploads.UploadOut | uploads.UploadErrorOut] | HttpResponse:
    """One chunk of a customer's file, as the public host read it off the wire."""
    if not _relay_ready():
        return _not_here()
    try:
        _user, organisation = _relay_actor(request)
        body = _relay_chunk_body(
            request, int(settings.CLOUD_UPLOAD_CHUNK_BYTES) + uploads.CHUNK_BODY_SLACK
        )
        job = uploads.append(organisation, upload_id, index, body, origin=MediaUpload.Origin.RELAY)
        return Status(200, uploads.out(job))
    except uploads.Refused as refused:
        return uploads.refusal(response, refused)


@internal_api.post(
    "/relay/uploads/{upload_id}/complete",
    auth=RelayToken(),
    response={
        200: uploads.UploadOut,
        400: uploads.UploadErrorOut,
        404: uploads.UploadErrorOut,
        409: uploads.UploadErrorOut,
    },
)
def relay_complete_upload(
    request: HttpRequest, upload_id: str, payload: uploads.CompleteUploadIn, response: HttpResponse
) -> Status[uploads.UploadOut | uploads.UploadErrorOut] | HttpResponse:
    """Close the upload and queue it for the worker."""
    if not _relay_ready():
        return _not_here()
    try:
        _user, organisation = _relay_actor(request)
        job = uploads.finish(
            organisation, upload_id, payload.sha256, origin=MediaUpload.Origin.RELAY
        )
        return Status(200, uploads.out(job))
    except uploads.Refused as refused:
        return uploads.refusal(response, refused)


@internal_api.get(
    "/relay/uploads",
    auth=RelayToken(),
    response={200: list[uploads.UploadOut], 404: uploads.UploadErrorOut},
)
def relay_list_uploads(
    request: HttpRequest, response: HttpResponse
) -> Status[list[uploads.UploadOut] | uploads.UploadErrorOut] | HttpResponse:
    """This organisation's uploads, for the screen the customer is looking at.

    Narrowed to the rows this credential created, like every other route here: on the public host a
    customer's uploads are relay rows by construction, so nothing real is hidden, and an upload the
    operator started over Tailscale stays out of a credential the internet can reach.
    """
    if not _relay_ready():
        return _not_here()
    try:
        _user, organisation = _relay_actor(request)
    except uploads.Refused as refused:
        return uploads.refusal(response, refused)
    rows = uploads.listing(organisation, origin=MediaUpload.Origin.RELAY)
    return Status(200, [uploads.out(row) for row in rows])


@internal_api.get(
    "/relay/uploads/{upload_id}",
    auth=RelayToken(),
    response={200: uploads.UploadOut, 404: uploads.UploadErrorOut},
)
def relay_read_upload(
    request: HttpRequest, upload_id: str, response: HttpResponse
) -> Status[uploads.UploadOut | uploads.UploadErrorOut] | HttpResponse:
    """One upload's state: the progress a relayed screen polls. A row this credential did not
    create is the same 404 as a stranger's."""
    if not _relay_ready():
        return _not_here()
    try:
        _user, organisation = _relay_actor(request)
        job = uploads.read_one(organisation, upload_id, origin=MediaUpload.Origin.RELAY)
        return Status(200, uploads.out(job))
    except uploads.Refused as refused:
        return uploads.refusal(response, refused)


@internal_api.delete(
    "/relay/uploads/{upload_id}",
    auth=RelayToken(),
    response={200: uploads.UploadOut, 404: uploads.UploadErrorOut},
)
def relay_cancel_upload(
    request: HttpRequest, upload_id: str, response: HttpResponse
) -> Status[uploads.UploadOut | uploads.UploadErrorOut] | HttpResponse:
    """Cancel an upload the relay created. The bytes go immediately."""
    if not _relay_ready():
        return _not_here()
    try:
        _user, organisation = _relay_actor(request)
        job = uploads.cancel(organisation, upload_id, origin=MediaUpload.Origin.RELAY)
        return Status(200, uploads.out(job))
    except uploads.Refused as refused:
        return uploads.refusal(response, refused)


@internal_api.post("/cache/purge", auth=None)
def cache_purge(request: HttpRequest) -> HttpResponse:
    """Apply a purge signed by development. 401 for anything that does not verify."""
    if settings.MARCELINE_ROLE != "prod":
        return _not_here()
    try:
        result = purge.receive_purge(request.body, store.verification_key(), now=timezone.now())
    except EnvelopeError as exc:
        logger.warning("cache purge refused: %s", exc.code)
        return JsonResponse({"detail": "rejected"}, status=401)
    return JsonResponse({"mode": result.mode, "bundles": result.bundles})


@internal_api.post("/outbox/pull", auth=None)
def outbox_pull(request: HttpRequest) -> HttpResponse:
    """Pending customer writes, signed by production, for a pull signed by development."""
    if settings.MARCELINE_ROLE != "prod" or not settings.OUTBOX_ENABLED:
        return _not_here()
    now = timezone.now()
    try:
        pull = outbox.verify_pull(request.body, store.verification_key(), now=now)
    except EnvelopeError as exc:
        logger.warning("outbox pull refused: %s", exc.code)
        return JsonResponse({"detail": "rejected"}, status=401)
    try:
        return _json_wire(outbox.signed_batch(pull, outbox.signing_key(), now=now))
    except EnvelopeError as exc:
        logger.error("outbox batch not signed: %s", exc.code)
        return JsonResponse({"detail": "outbox_unavailable"}, status=503)


@internal_api.post("/outbox/ack", auth=None)
def outbox_ack(request: HttpRequest) -> HttpResponse:
    """Settle customer writes with the outcomes development signed."""
    if settings.MARCELINE_ROLE != "prod" or not settings.OUTBOX_ENABLED:
        return _not_here()
    now = timezone.now()
    try:
        payload = outbox.verify_ack(request.body, store.verification_key(), now=now)
    except EnvelopeError as exc:
        logger.warning("outbox acknowledgement refused: %s", exc.code)
        return JsonResponse({"detail": "rejected"}, status=401)
    result = outbox.apply_ack(payload, now=now)
    return JsonResponse({"settled": result.settled, "ignored": result.ignored})
