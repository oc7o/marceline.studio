"""`/api/v1/app/uploads`: cloud fingerprinting (issue #59, ADR 0044) — the customer sends the file.

The opposite direction of `fingerprints.py`, which takes hashes a browser computed. Here the file
itself arrives, in fixed chunks, is fingerprinted by `fingerprint-worker` and is deleted. The
customer chooses this per file; local hashing stays the default and the visible one.

**Development, or a relay.** `/api/v1/app/uploads` is in `public_surface.ROLE_ONLY_PREFIXES["dev"]`,
so on `MARCELINE_ROLE=prod` every route here answers the same bare 404 as an unknown path — unless
`CLOUD_UPLOAD_RELAY_MODE` is set, in which case each request is **forwarded to development as it is
read** and the public host stores nothing (ADR 0044 §9, amendment 2026-09-19,
`marceline/uploads/relay.py`). Customer media still never rests on the public host: no spool (the
settings refuse one there), no row, no outbox item, no temporary file.

**Off by default.** Without `CLOUD_FINGERPRINTING_ENABLED` every route answers 404 and
`me.can_upload_media` is false. The switch stays off until the preconditions of the ADR 0044
amendment of 2026-09-19 are met (issue #124).

Three writes and three reads:

    POST   /api/v1/app/uploads                     declare one upload; everything refusable
                                                   without the file is refused here
    POST   /api/v1/app/uploads/{id}/chunks/{n}     one chunk, raw bytes, at its own offset
    POST   /api/v1/app/uploads/{id}/complete       the customer's sha256 against ours; queue it
    GET    /api/v1/app/uploads                     this organisation's uploads
    GET    /api/v1/app/uploads/{id}                one of them: where it is, how far, what became
                                                   of it — what the screen polls (ADR 0044
                                                   amendment 2026-09-18)
    DELETE /api/v1/app/uploads/{id}                cancel; the bytes go immediately

Every refusal is a code. A malformed, unknown or another organisation's `upl_…` is the same 404
(ADR 0021), and the customer's **file name is never sent**: the browser uses it to pre-fill the
title, and the title is the customer's own words.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from functools import wraps
from typing import Any, Literal

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from ninja import Field, Router, Schema, Status
from ninja.decorators import decorate_view

from accounts.models import Organisation
from core import public_ids
from marceline.app import upload_declaration, upload_throttle
from marceline.app.auth import customer_auth
from marceline.app.ownership import membership_of
from marceline.app.rights import CURRENT_VERSION, LANGUAGES
from marceline.models import MediaUpload
from marceline.uploads import relay, spool
from matching.fingerprint import MAX_CREATORS, MAX_TITLE_LENGTH

logger = logging.getLogger("marceline.app.uploads")

router = Router(tags=["app"], auth=customer_auth)

#: Sampling rates an upload may ask for — the same set the browser endpoint accepts.
SUPPORTED_FPS = frozenset({1.0, 2.0})
#: Slack above the chunk size for a request body, so a chunk is never refused by a byte.
CHUNK_BODY_SLACK = 1024
#: Uploads a customer sees at once.
MAX_LISTED = 50
#: The rolling window the per-organisation byte quota is counted over.
QUOTA_WINDOW = timedelta(hours=24)

#: Why an upload did not become an original, as the **customer's** screen may put it — a closed
#: set the UI has a sentence for in both languages, deliberately coarser than `result_code`
#: (ADR 0044 amendment 2026-09-18). Anything unmapped reads `internal`, so a new code added
#: anywhere can never appear as an unworded string on a customer's screen.
#:
#: There is no reason here that names a file, a path, a decoder's complaint or a container's own
#: words: the whole point of a code is that the sentence is ours and the file's is never repeated.
FailureReason = Literal[
    "unsupported_format",
    "too_long",
    "too_large",
    "decode_failed",
    "timed_out",
    "quota",
    "incomplete",
    "needs_review",
    "cancelled",
    "internal",
]

#: `MediaUpload.result_code` → what the customer is told. The left side is every code
#: `uploads/service.py`, `matching/cloud_hash.py` and this module can settle a job with.
FAILURE_REASONS: dict[str, FailureReason] = {
    # The probe's allowlist: the container or the codec is not one we decode, or there is no
    # video in the file at all.
    "unsupported_container": "unsupported_format",
    "unsupported_codec": "unsupported_format",
    "no_video_stream": "unsupported_format",
    "unreadable_file": "unsupported_format",
    "unsupported_fingerprint": "unsupported_format",
    "invalid_fingerprint": "unsupported_format",
    # Too much of something.
    "too_long": "too_long",
    "frame_too_large": "too_large",
    "too_many_streams": "too_large",
    "too_large": "too_large",
    # The decoder started and did not finish.
    "decode_failed": "decode_failed",
    "decode_timeout": "timed_out",
    "probe_timeout": "timed_out",
    # Limits and the upload itself.
    "quota_exceeded": "quota",
    "spool_full": "quota",
    "too_many_requests": "quota",
    "digest_mismatch": "incomplete",
    "expired": "incomplete",
    "cancelled": "cancelled",
    # Registration refused it: another organisation has claimed this file, or the declaration the
    # customer ticked was retired while the file was being hashed. Both are for a person.
    "claim_needs_review": "needs_review",
    "rights_confirmation_required": "needs_review",
    "upload_declaration_required": "needs_review",
    "no_organisation": "needs_review",
}


def failure_reason(job: MediaUpload) -> FailureReason | None:
    """Why this upload did not become an original, or None while it still might."""
    if job.status not in (
        MediaUpload.Status.REFUSED,
        MediaUpload.Status.FAILED,
        MediaUpload.Status.EXPIRED,
        MediaUpload.Status.CANCELLED,
    ):
        return None
    return FAILURE_REASONS.get(job.result_code, "internal")


ErrorCode = Literal[
    "uploads_unavailable",
    "no_organisation",
    "too_many_requests",
    "quota_exceeded",
    "spool_full",
    "invalid_request",
    "too_large",
    "rights_confirmation_required",
    "upload_declaration_required",
    "out_of_order",
    "digest_mismatch",
    "wrong_state",
    "body_too_large",
    "not_found",
]


class StartUploadIn(Schema):
    file_size: int = Field(description="The file's size in bytes, as the browser reports it.")
    title: str = Field(
        max_length=MAX_TITLE_LENGTH,
        description="What the customer calls this original. Pre-filled from the file name in the "
        "browser and editable there; the file name itself is never sent.",
    )
    creators: list[str] = Field(
        description="The cast, the rights holder first — the same list a local fingerprint carries."
    )
    fps: float = Field(description="Sampling rate; one of the supported ones.")
    rights_confirmed: bool = Field(description="The rights declaration was ticked. Must be true.")
    rights_text_version: str = Field(max_length=32, description="Must be the current version.")
    language: Literal["de", "en"]
    sole_performer_confirmed: bool = Field(
        default=False,
        description="The uploader declared they are the only person in the file (ADR 0044 "
        "amendment 2026-09-19). Must be true: a file with other people in it is fingerprinted in the "
        "browser instead, where the bytes never leave the machine.",
    )
    article9_consent: bool = Field(
        default=False,
        description="Explicit consent under the GDPR to processing this file in order to "
        "compute fingerprints from it. Must be true, and is a separate tick from the declaration "
        "above on purpose.",
    )
    upload_declaration_version: str = Field(
        default="",
        max_length=48,
        description="The version of the upload declaration that was shown. Must be the current "
        "one; the server stores its text verbatim with the upload.",
    )


class CompleteUploadIn(Schema):
    sha256: str = Field(
        max_length=64, description="The file's SHA-256, as the browser computed it."
    )


class UploadOut(Schema):
    id: str = Field(description="Opaque id of this upload (`upl_…`).")
    title: str
    status: Literal[
        "receiving", "queued", "hashing", "done", "refused", "failed", "expired", "cancelled"
    ]
    code: str = Field(description="Why it ended as it did; empty while it is running.")
    reason: FailureReason | None = Field(
        description="Why it did not become an original, from a closed set the app has words for; "
        "null while it may still become one."
    )
    original_id: str | None = Field(description="The original it became (`orig_…`), or null.")
    declared_bytes: int
    received_bytes: int
    chunk_bytes: int
    next_chunk: int = Field(description="The chunk index to send next; resume from here.")
    frames_done: int = Field(description="Sampled moments hashed so far; 0 before hashing starts.")
    frames_total: int | None = Field(
        description="How many moments are expected, once the file has been probed — an estimate, "
        "and null while nothing knows it. Never turn it into a time."
    )
    bytes_deleted: bool = Field(
        description="Whether the uploaded file is verifiably gone. True only from a deletion this "
        "server checked afterwards, so the app may say the file was deleted exactly when it is."
    )
    created_at: Any


class UploadErrorOut(Schema):
    detail: ErrorCode
    message: str = Field(default="", description="Technical reason; never names anyone else.")


class Refused(Exception):
    """A refusal with a status and one of this module's codes. Its own class rather than the
    fingerprint endpoint's, because the two answer different codes."""

    def __init__(self, status: int, code: ErrorCode, message: str = "") -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


def refusal(response: HttpResponse, refused: Refused) -> Status[Any]:
    if refused.status == 429:
        response["Retry-After"] = refused.message
        return Status(429, UploadErrorOut(detail=refused.code))
    return Status(refused.status, UploadErrorOut(detail=refused.code, message=refused.message))


#: A spool refusal, as a code this endpoint answers. Anything unexpected reads as a plain refusal
#: rather than leaking a new word into the API.
SPOOL_CODES: dict[str, ErrorCode] = {
    "unavailable": "uploads_unavailable",
    "unknown_upload": "not_found",
    "out_of_order": "out_of_order",
    "too_large": "too_large",
    "spool_full": "spool_full",
}


def spool_code(code: str) -> ErrorCode:
    return SPOOL_CODES.get(code, "invalid_request")


def uploads_available() -> bool:
    """Whether this machine takes files at all: the switch, and somewhere for them to go.

    On the public host that "somewhere" is never here: with `CLOUD_UPLOAD_RELAY_MODE` the bytes are
    forwarded to development as they arrive (ADR 0044 §9) and this process has no spool at all —
    `core/deploy.cloud_upload_problems` refuses to start one that has both.
    """
    if not settings.CLOUD_FINGERPRINTING_ENABLED:
        return False
    if settings.CLOUD_UPLOAD_RELAY_MODE:
        return relay.configured()
    return spool.available()


def allow_chunk_body(run: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    """Read a signed-in caller's chunk past Django's global body limit, and no further.

    The same shape as `fingerprints.allow_large_body`: an anonymous caller is left to the session
    auth with the body unread, and a larger body is refused with 413 before a byte is read.
    """

    @wraps(run)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if relay.active():
            # The relay reads the body itself, in pieces, and hands each one straight on. Nothing
            # may materialise it here — not even to check its length against a cap this process no
            # longer owns (development's cap is the one that counts).
            return run(request, *args, **kwargs)
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        limit = int(settings.CLOUD_UPLOAD_CHUNK_BYTES) + CHUNK_BODY_SLACK
        global_limit = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        if request.user.is_authenticated and global_limit is not None and length > global_limit:
            if length > limit:
                return JsonResponse({"detail": "body_too_large", "message": ""}, status=413)
            request._body = request.read(length)
        return run(request, *args, **kwargs)

    return wrapped


def _customer(request: HttpRequest) -> tuple[User, Organisation]:
    if not uploads_available():
        raise Refused(404, "not_found")
    user = request.user
    membership = membership_of(user)
    if membership is None or not isinstance(user, User):
        raise Refused(403, "no_organisation")
    return user, membership.organisation


def out(job: MediaUpload) -> UploadOut:
    received = job.received_bytes
    return UploadOut(
        id=job.public_id,
        title=job.title,
        status=job.status,  # type: ignore[arg-type]
        code=job.result_code,
        reason=failure_reason(job),
        original_id=job.original_id or None,
        declared_bytes=job.declared_bytes,
        received_bytes=received,
        chunk_bytes=job.chunk_bytes,
        next_chunk=received // job.chunk_bytes,
        frames_done=job.frames_done,
        frames_total=job.frames_total,
        bytes_deleted=job.bytes_deleted_at is not None,
        created_at=job.created_at,
    )


def _job(organisation: Organisation, upload_id: str, *, origin: str | None = None) -> MediaUpload:
    """One of this organisation's uploads. Malformed, unknown and foreign are the same 404.

    `origin` narrows it to uploads that arrived that way. The relay passes
    `MediaUpload.Origin.RELAY`, which is how "the credential may append to an upload **it created**"
    (ADR 0044 §9) is enforced by a query rather than by a promise: a row the operator started over
    Tailscale is, to the relay, the same 404 as another organisation's.
    """
    if not public_ids.is_valid("upl", upload_id):
        raise Refused(404, "not_found")
    rows = MediaUpload.objects.filter(organisation=organisation, public_id=upload_id)
    if origin is not None:
        rows = rows.filter(origin=origin)
    job = rows.first()
    if job is None:
        raise Refused(404, "not_found")
    return job


def used_bytes(organisation: Organisation, *, now: Any = None) -> int:
    """Bytes this organisation has declared in the rolling window, cancellations excluded."""
    moment = now or timezone.now()
    rows = MediaUpload.objects.filter(
        organisation=organisation, created_at__gte=moment - QUOTA_WINDOW
    ).exclude(status=MediaUpload.Status.CANCELLED)
    return sum(row.declared_bytes for row in rows.only("declared_bytes"))


def validated(payload: StartUploadIn) -> None:
    """Everything that can be refused before a single byte arrives. Raises `Refused`."""
    if (
        not payload.rights_confirmed
        or payload.rights_text_version != CURRENT_VERSION
        or payload.language not in LANGUAGES
    ):
        raise Refused(400, "rights_confirmation_required")
    # The upload declaration (ADR 0044 amendment 2026-09-19): the feature rests on the uploader's
    # own explicit consent about their own data, so an upload without both ticks, or naming a
    # wording that is not the current one, is refused before a byte is asked for.
    if (
        not payload.sole_performer_confirmed
        or not payload.article9_consent
        or payload.upload_declaration_version != upload_declaration.CURRENT_VERSION
        or payload.language not in upload_declaration.LANGUAGES
    ):
        raise Refused(400, "upload_declaration_required")
    if payload.file_size <= 0 or payload.file_size > settings.CLOUD_UPLOAD_MAX_BYTES:
        raise Refused(400, "too_large", "the file is larger than this account may upload")
    if payload.fps not in SUPPORTED_FPS:
        raise Refused(400, "invalid_request", f"fps must be one of {sorted(SUPPORTED_FPS)}")
    names = [" ".join(name.split()) for name in payload.creators]
    if not names or len(names) > MAX_CREATORS or not all(names):
        raise Refused(400, "invalid_request", "name the rights holder")
    if not all(name.isprintable() for name in names):
        raise Refused(400, "invalid_request", "creator names must be printable text")
    if not payload.title.strip():
        raise Refused(400, "invalid_request", "a title is required")


# ---------------------------------------------------------------------------------------------
# The six operations, once. The customer routes below call them, and so do the relay routes on
# development (`marceline/internal_api.py`) when the public host forwards a request — never a second
# implementation, exactly as `register_fingerprint` is the one registration function.
# ---------------------------------------------------------------------------------------------


def begin(
    user: User, organisation: Organisation, payload: StartUploadIn, *, origin: str
) -> MediaUpload:
    """Declare one upload. Everything refusable without the file is refused here."""
    validated(payload)
    refusal = upload_throttle.admit(organisation.pk, upload_throttle.Kind.ORIGINAL)
    if refusal is not None:
        raise Refused(429, "too_many_requests", str(refusal.retry_after_s))
    if used_bytes(organisation) + payload.file_size > settings.CLOUD_UPLOAD_DAILY_BYTES_PER_ORG:
        raise Refused(503, "quota_exceeded")
    if spool.total_bytes() + payload.file_size > settings.CLOUD_UPLOAD_SPOOL_MAX_BYTES:
        raise Refused(503, "spool_full")
    job = MediaUpload.objects.create(
        organisation=organisation,
        created_by=user,
        origin=origin,
        declared_bytes=payload.file_size,
        title=payload.title.strip()[:MAX_TITLE_LENGTH],
        creators=[" ".join(name.split()) for name in payload.creators],
        frame_fps=payload.fps,
        chunk_bytes=int(settings.CLOUD_UPLOAD_CHUNK_BYTES),
        rights_text_version=payload.rights_text_version,
        rights_language=payload.language,
        upload_declaration_version=payload.upload_declaration_version,
        upload_declaration_language=payload.language,
        upload_declaration_text=upload_declaration.snapshot(
            payload.upload_declaration_version, payload.language
        ),
        upload_declaration_at=timezone.now(),
    )
    try:
        spool.create(job.public_id)
    except spool.SpoolError as error:
        raise Refused(503, "uploads_unavailable", error.code) from error
    logger.info("organisation %s started upload %s", organisation.pk, job.public_id)
    return job


def append(
    organisation: Organisation,
    upload_id: str,
    index: int,
    body: bytes,
    *,
    origin: str | None = None,
) -> MediaUpload:
    """Write chunk `index` at its own offset and answer the upload's new state."""
    job = _job(organisation, upload_id, origin=origin)
    if job.status != MediaUpload.Status.RECEIVING:
        raise Refused(409, "wrong_state", "this upload is not receiving chunks")
    try:
        size = spool.append(
            job.public_id,
            index,
            body,
            chunk_bytes=job.chunk_bytes,
            max_bytes=job.declared_bytes,
        )
    except spool.SpoolError as error:
        status = {"too_large": 413, "unknown_upload": 404}.get(error.code, 409)
        raise Refused(status, spool_code(error.code)) from error
    MediaUpload.objects.filter(pk=job.pk).update(received_bytes=size)
    job.received_bytes = size
    return job


def finish(
    organisation: Organisation, upload_id: str, sha256: str, *, origin: str | None = None
) -> MediaUpload:
    """Close the upload: our own digest of what we assembled must be the one the customer sent."""
    job = _job(organisation, upload_id, origin=origin)
    if job.status != MediaUpload.Status.RECEIVING:
        raise Refused(409, "wrong_state", "this upload is not receiving chunks")
    try:
        assembled = spool.assemble(job.public_id)
    except spool.SpoolError as error:
        raise Refused(
            404 if error.code == "unknown_upload" else 409, spool_code(error.code)
        ) from error
    if assembled.size_bytes != job.declared_bytes or assembled.sha256 != sha256.strip().lower():
        spool.delete(job.public_id)
        with transaction.atomic():
            MediaUpload.objects.filter(pk=job.pk).update(
                status=MediaUpload.Status.REFUSED,
                result_code="digest_mismatch",
                received_bytes=assembled.size_bytes,
                bytes_deleted_at=timezone.now(),
            )
        raise Refused(400, "digest_mismatch")
    MediaUpload.objects.filter(pk=job.pk).update(
        status=MediaUpload.Status.QUEUED,
        sha256=assembled.sha256,
        declared_sha256=assembled.sha256,
        received_bytes=assembled.size_bytes,
        completed_at=timezone.now(),
    )
    job.refresh_from_db()
    logger.info("upload %s is complete and queued", job.public_id)
    return job


def listing(organisation: Organisation, *, origin: str | None = None) -> list[MediaUpload]:
    """This organisation's uploads, newest first.

    A browser passes no `origin` and sees them all. The relay passes `MediaUpload.Origin.RELAY`
    (ADR 0044 §9, amendment 2026-09-20): a credential the internet can reach sees only the rows it
    created itself, reads included, and on the public host those are all of a customer's anyway.
    """
    rows = MediaUpload.objects.filter(organisation=organisation)
    if origin is not None:
        rows = rows.filter(origin=origin)
    return list(rows.order_by("-created_at", "-pk")[:MAX_LISTED])


def read_one(
    organisation: Organisation, upload_id: str, *, origin: str | None = None
) -> MediaUpload:
    """One upload's state. `origin` narrows it the same way `listing` is narrowed."""
    return _job(organisation, upload_id, origin=origin)


def cancel(organisation: Organisation, upload_id: str, *, origin: str | None = None) -> MediaUpload:
    """Cancel an upload. The bytes go immediately; a job already being hashed is left alone."""
    job = _job(organisation, upload_id, origin=origin)
    if job.status in (MediaUpload.Status.RECEIVING, MediaUpload.Status.QUEUED):
        deleted = spool.delete(job.public_id)
        MediaUpload.objects.filter(pk=job.pk).update(
            status=MediaUpload.Status.CANCELLED,
            result_code="cancelled",
            bytes_deleted_at=timezone.now() if deleted else None,
        )
        job.refresh_from_db()
    return job


@router.post(
    "",
    response={
        201: UploadOut,
        400: UploadErrorOut,
        403: UploadErrorOut,
        404: UploadErrorOut,
        429: UploadErrorOut,
        503: UploadErrorOut,
    },
)
def start_upload(
    request: HttpRequest, payload: StartUploadIn, response: HttpResponse
) -> Status[UploadOut | UploadErrorOut] | HttpResponse:
    """Declare one upload and get the chunk size to send it in.

    201 created · 400 invalid, or a declaration is missing or out of date · 403 no organisation ·
    404 uploads are not available on this machine · 429 per-organisation limit · 503 this account's
    daily byte quota or the spool is full.
    """
    try:
        user, organisation = _customer(request)
        if relay.active():
            return relay.forward_json(
                request,
                "/uploads",
                method="POST",
                organisation_id=organisation.pk,
                user_id=user.pk,
            )
        job = begin(user, organisation, payload, origin=MediaUpload.Origin.DIRECT)
        return Status(201, out(job))
    except Refused as refused:
        return refusal(response, refused)


@router.post(
    "/{upload_id}/chunks/{int:index}",
    response={
        200: UploadOut,
        403: UploadErrorOut,
        404: UploadErrorOut,
        409: UploadErrorOut,
        413: UploadErrorOut,
    },
)
@decorate_view(allow_chunk_body)
def put_chunk(
    request: HttpRequest, upload_id: str, index: int, response: HttpResponse
) -> Status[UploadOut | UploadErrorOut] | HttpResponse:
    """Send chunk `index`, raw bytes. Re-sending the last one overwrites it; anything else is 409.

    Resume by reading `next_chunk` from the upload's status and continuing there.

    On the public host this is the one request that carries a customer's media, and it is
    **streamed** to development and never held (`marceline/uploads/relay.py`): the body is read in
    64 KiB pieces and each piece is handed on as it is read. `request.body` is not touched on that
    path, which is also what keeps Django's multipart handlers — the only thing in Django that
    writes an upload to a temporary file — out of it entirely.
    """
    try:
        user, organisation = _customer(request)
        if relay.active():
            return relay.forward_chunk(
                request,
                f"/uploads/{upload_id}/chunks/{int(index)}",
                organisation_id=organisation.pk,
                user_id=user.pk,
            )
        return Status(200, out(append(organisation, upload_id, index, request.body)))
    except Refused as refused:
        return refusal(response, refused)


@router.post(
    "/{upload_id}/complete",
    response={
        200: UploadOut,
        400: UploadErrorOut,
        403: UploadErrorOut,
        404: UploadErrorOut,
        409: UploadErrorOut,
    },
)
def complete_upload(
    request: HttpRequest, upload_id: str, payload: CompleteUploadIn, response: HttpResponse
) -> Status[UploadOut | UploadErrorOut] | HttpResponse:
    """Close the upload: the server's own digest of what it assembled must be the one sent.

    400 `digest_mismatch` (the bytes on disk are not the file the customer hashed — the spool is
    emptied and nothing is queued) · 409 the upload is not receiving chunks.
    """
    try:
        user, organisation = _customer(request)
        if relay.active():
            return relay.forward_json(
                request,
                f"/uploads/{upload_id}/complete",
                method="POST",
                organisation_id=organisation.pk,
                user_id=user.pk,
            )
        return Status(200, out(finish(organisation, upload_id, payload.sha256)))
    except Refused as refused:
        return refusal(response, refused)


@router.get("", response={200: list[UploadOut], 403: UploadErrorOut, 404: UploadErrorOut})
def list_uploads(
    request: HttpRequest, response: HttpResponse
) -> Status[list[UploadOut] | UploadErrorOut] | HttpResponse:
    """This organisation's uploads, newest first."""
    try:
        user, organisation = _customer(request)
        if relay.active():
            return relay.forward_json(
                request,
                "/uploads",
                method="GET",
                organisation_id=organisation.pk,
                user_id=user.pk,
            )
    except Refused as refused:
        return refusal(response, refused)
    return Status(200, [out(row) for row in listing(organisation)])


@router.get("/{upload_id}", response={200: UploadOut, 403: UploadErrorOut, 404: UploadErrorOut})
def read_upload(
    request: HttpRequest, upload_id: str, response: HttpResponse
) -> Status[UploadOut | UploadErrorOut] | HttpResponse:
    """One upload's state: where to resume, how far the fingerprinting has come, what it became.

    This is what the add-original screen polls after the last chunk (ADR 0044 amendment
    2026-09-18). `status` moves `receiving → queued → hashing → done | refused | failed`,
    `frames_done`/`frames_total` are the bar (a null total means "not known yet", never a guessed
    denominator), `reason` is the closed set the app has words for, `original_id` is the original
    to link to once there is one, and `bytes_deleted` says whether the file is verifiably gone —
    which is the only thing that entitles the screen to say it was deleted.
    """
    try:
        user, organisation = _customer(request)
        if relay.active():
            return relay.forward_json(
                request,
                f"/uploads/{upload_id}",
                method="GET",
                organisation_id=organisation.pk,
                user_id=user.pk,
            )
        return Status(200, out(read_one(organisation, upload_id)))
    except Refused as refused:
        return refusal(response, refused)


@router.delete("/{upload_id}", response={200: UploadOut, 403: UploadErrorOut, 404: UploadErrorOut})
def cancel_upload(
    request: HttpRequest, upload_id: str, response: HttpResponse
) -> Status[UploadOut | UploadErrorOut] | HttpResponse:
    """Cancel an upload. The bytes go immediately; a job already being hashed is left alone."""
    try:
        user, organisation = _customer(request)
        if relay.active():
            return relay.forward_json(
                request,
                f"/uploads/{upload_id}",
                method="DELETE",
                organisation_id=organisation.pk,
                user_id=user.pk,
            )
        return Status(200, out(cancel(organisation, upload_id)))
    except Refused as refused:
        return refusal(response, refused)
