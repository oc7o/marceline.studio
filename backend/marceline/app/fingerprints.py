"""`POST /api/v1/app/fingerprints`: a customer adds an original from fingerprints computed locally.

The files never leave the customer's computer. The app's "Add original" page
(`frontend/src/app/AddOriginalScreen.tsx`) digests and hashes them in the browser and sends exactly
the document `matching.fingerprint.to_json` describes — sha256, size, duration or image count,
sampling rate, title, cast and PDQ hashes — plus the customer's rights declaration. Nothing else is
accepted, and never a file name.

An original is a **video** or a **photo collection** (`kind`, ADR 0020; issue #116, ADR 0053).
Both take this one path: only the recipe tag, the size cap and the refusal code that names it
differ (`_validated_shape`). A collection has no duration and no sampling rate at all — the
database enforces that — so a photo document carries `image_count` and no time anywhere.

The import is the one every other path uses: `from_json` validates, `import_fingerprint` stores the
reference and its hashes atomically, `ownership.register` records the owner. What this module adds
is only what a *customer* upload needs on top of the staff endpoint (`marceline/originals.py`):

- **The owner is the caller's organisation**, never a field of the request.
- **A rights declaration is required** (`rights.py`): the current wording, ticked, stored as a
  `RightsConfirmation` with who, when, version and language, and the work's rights record (ADR 0034)
  becomes `self_declared` through `rights.services.declare_at_upload`. It is a claim, not proof.
- **Stricter shape.** A browser upload is a video on `BROWSER_PIPELINE` at a supported sampling
  rate with at most `MAX_POSITIONS` sampled positions, or a photo collection on
  `BROWSER_PHOTO_PIPELINE` with at most `MAX_IMAGES` images; always all eight orientations and a
  rights holder named. The body limit is raised for this route only (`MAX_BODY_BYTES`).
- **Idempotent.** The same file again from the same organisation answers 200 with the original it
  already has, whatever recipe or rate registered it first — a customer re-adding a video means
  "this one", not "hash it again beside itself". Only a first registration answers 201. The
  database identity (sha256, pipeline, fps) is still what `import_fingerprint` enforces.
- **Claims on someone else's material stop at a human.** A file registered by another organisation
  or by the operator, or hashes that largely duplicate such an original, answer one generic 409
  that names nobody and says nothing about which case applied; the details go to the log for
  staff. Deciding who holds the rights is not something a server can do (ADR 0019).
- **The cast is mapped conservatively.** A name equal to an existing creator's name links it; any
  other name becomes a new creator marked `needs_review`, which is not crawled, linked or searched
  until staff have looked at it. Aliases are never matched here: they hold studio names, and a
  customer typing a studio must not silently become some performer's claim.
- **Rate-limited per organisation** (`upload_throttle.py`).
- **On production, through the outbox** (ADR 0028 amendment "The production write path").
  Production has no originals to write to. With `OUTBOX_ENABLED` it runs every check above that
  needs no originals — session, CSRF, rate limit, `validated` — and stores the request in
  `delivery.OutboxItem`, answering **202** with a submission the app shows as "being added";
  development pulls it and applies it with `register_original`, the same function this endpoint
  runs there (`marceline/customer_writes.py`), and the outcome reaches the customer through
  `GET /app/fingerprints/submissions` and the refreshed cache. Without the outbox production
  answers 503 and `me.can_add_originals` is false; when development has not pulled for
  `OUTBOX_PAUSE_AFTER_HOURS` it answers 503 `processing_paused`.

Nothing here matches, confirms or notices anything: one reference, its hashes, its owner and the
declaration, and nothing else.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any, Literal, get_args

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from ninja import Field, Router, Schema, Status
from ninja.decorators import decorate_view

from accounts.models import Organisation
from catalog.models import Creator
from delivery import outbox
from delivery.models import OutboxItem
from marceline.app import upload_throttle
from marceline.app.auth import customer_auth
from marceline.app.originals import confirmed_findings_of, original_out
from marceline.app.ownership import (
    OwnedOriginal,
    Via,
    group_originals,
    membership_of,
    owned_references,
    public_ids_for,
    register,
)
from marceline.app.rights import CURRENT_VERSION, LANGUAGES, text_of
from marceline.app.schemas import CustomerOriginalOut
from marceline.models import RefusedClaim, RightsConfirmation
from marceline.originals import DUPLICATE_CLAIM_MIN_SHARE, SOURCE, overlap_shares
from marceline.rights import services as rights_services
from matching.fingerprint import (
    BROWSER_PHOTO_PIPELINE,
    BROWSER_PIPELINE,
    KIND_PHOTOS,
    KIND_VIDEO,
    Fingerprint,
    FingerprintFormatError,
    from_json,
)
from matching.models import ClientEvent, Reference
from matching.references import import_fingerprint

logger = logging.getLogger("marceline.app.fingerprints")

router = Router(tags=["app"], auth=customer_auth)

#: Sampling rates the app may send: the default 1 fps and the denser 2 fps grid the operator is
#: piloting (ADR 0022 amendment). The app's own default lives in one place,
#: `frontend/src/lib/videoFingerprint.ts` `DEFAULT_FPS`, and must be one of these.
SUPPORTED_FPS = frozenset({1.0, 2.0})
#: Sampled positions per upload: three hours at 1 fps, ninety minutes at 2 fps. Mirrored by
#: `MAX_UPLOAD_POSITIONS` in the frontend, which stops before hashing a longer video.
MAX_POSITIONS = 10_800
#: Images per photo collection (issue #116, ADR 0053). A photo frame row carries no `timestamp_s`,
#: so it is about 105 bytes of JSON and one image is eight of them: 5,000 images are roughly
#: 4.4 MB, the same headroom the video cap leaves inside `MAX_BODY_BYTES`. The CLI's
#: `MAX_COLLECTION_IMAGES` (20,000) stays what it is — a local import has no HTTP body. Mirrored by
#: `MAX_UPLOAD_IMAGES` in the frontend, which stops before hashing a larger set.
MAX_IMAGES = 5_000
#: One position is eight rows of about 145 bytes of JSON, so the largest valid document is about
#: 12.5 MB. Django's global limit (2.5 MB) stays for every other route.
MAX_BODY_BYTES = 16 * 1024 * 1024
#: A file size no video original reaches; anything larger is a malformed document.
MAX_FILE_SIZE = 1 << 42
#: Second key space for the per-file advisory lock (first key: this constant).
FILE_LOCK_NAMESPACE = 0x10616
#: `Retry-After` while production's outbox is paused: development is expected back within hours.
PAUSED_RETRY_AFTER_S = 3600
#: What `Reference.source_note` records for a browser upload. Cloud fingerprinting (ADR 0044)
#: passes its own, so the two are distinguishable without a second registration path.
SOURCE_NOTE = "Added by the customer in the app"

ErrorCode = Literal[
    "adding_originals_unavailable",
    "processing_paused",
    "no_organisation",
    "too_many_requests",
    "body_too_large",
    "rights_confirmation_required",
    "invalid_fingerprint",
    "unsupported_fingerprint",
    "too_long",
    "too_many_images",
    "claim_needs_review",
]


class AddOriginalIn(Schema):
    fingerprint: dict[str, Any] = Field(
        description="A `marceline-fingerprint` document as `matching.fingerprint.to_json` writes "
        "it — a video or a photo collection (`kind`): hashes and metadata only, never file data. "
        "`creator` names the rights holder first, then co-performers."
    )
    rights_confirmed: bool = Field(
        description="The customer ticked the rights declaration. Must be true."
    )
    rights_text_version: str = Field(
        max_length=32, description="Version of the declaration shown; must be the current one."
    )
    language: Literal["de", "en"] = Field(description="Language the declaration was shown in.")


class AddOriginalOut(Schema):
    original: CustomerOriginalOut
    created: bool = Field(
        description="False when this organisation already had the file: nothing was imported."
    )


class AddOriginalErrorOut(Schema):
    detail: ErrorCode
    message: str = Field(
        default="",
        description="Technical reason for `invalid_fingerprint` / `unsupported_fingerprint` / "
        "`too_long`; empty otherwise. Never names another organisation or original.",
    )


SubmissionStatus = Literal["processing", "done", "refused", "failed"]
#: What became of a submission: `created` / `already_present` when it was added, a refusal code of
#: this endpoint when it was not, `processing_failed` when it could not be processed automatically
#: and waits for a person, and empty while it is processing.
SubmissionCode = Literal[
    "",
    "created",
    "already_present",
    "no_organisation",
    "rights_confirmation_required",
    "invalid_fingerprint",
    "unsupported_fingerprint",
    "too_long",
    "too_many_images",
    "claim_needs_review",
    "processing_failed",
]
SUBMISSION_CODES: frozenset[str] = frozenset(get_args(SubmissionCode))
SUBMISSION_STATUS: dict[str, SubmissionStatus] = {
    OutboxItem.Status.PENDING: "processing",
    OutboxItem.Status.DONE: "done",
    OutboxItem.Status.REFUSED: "refused",
    OutboxItem.Status.QUARANTINED: "failed",
}
#: Submissions the app lists (settled ones disappear after `delivery.outbox.RETENTION`).
MAX_SUBMISSIONS = 50


class SubmissionOut(Schema):
    id: str = Field(description="Opaque id of this submission (`sub_…`).")
    title: str = Field(description="The title the original was sent with.")
    status: SubmissionStatus = Field(
        description="`processing` until it has been added or refused; `failed` means it could not "
        "be processed automatically and a person will look at it."
    )
    code: SubmissionCode
    original_id: str | None = Field(
        description="The original's id once it has been added (`orig_…`); null otherwise."
    )
    received_at: datetime


class AddOriginalAcceptedOut(Schema):
    submission: SubmissionOut


class AppClientEventIn(Schema):
    kind: Literal["codec_unsupported", "client_failed"]
    detail: str = Field(default="", max_length=ClientEvent.DETAIL_LENGTH)
    container: str = Field(default="", max_length=ClientEvent.SHORT_LENGTH)
    codec: str = Field(default="", max_length=ClientEvent.SHORT_LENGTH)


class RecordedOut(Schema):
    recorded: bool


class Refused(Exception):
    def __init__(self, status: int, code: ErrorCode, message: str = "") -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


def adding_originals_available() -> bool:
    """Whether this machine takes customer uploads: development directly, production only into
    the outbox (`OUTBOX_ENABLED`)."""
    return settings.MARCELINE_ROLE != "prod" or outbox.writes_enabled()


def into_outbox() -> bool:
    """Writes here are accepted into the outbox rather than applied (production)."""
    return settings.MARCELINE_ROLE == "prod"


def allow_large_body(run: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    """Read a signed-in caller's body up to `MAX_BODY_BYTES`, past Django's global limit.

    Runs before the operation's own checks: an anonymous caller is left to the session auth (401,
    body unread), and a larger body is refused with 413 before a byte of it is read.
    """

    @wraps(run)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        global_limit = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        if request.user.is_authenticated and global_limit is not None and length > global_limit:
            if length > MAX_BODY_BYTES:
                return JsonResponse({"detail": "body_too_large", "message": ""}, status=413)
            request._body = request.read(length)
        return run(request, *args, **kwargs)

    return wrapped


def _organisation(request: HttpRequest, kind: str) -> tuple[User, Organisation]:
    if not adding_originals_available():
        raise Refused(503, "adding_originals_unavailable")
    user = request.user
    membership = membership_of(user)
    if membership is None or not isinstance(user, User):
        raise Refused(403, "no_organisation")
    if into_outbox() and outbox.paused(timezone.now()):
        raise Refused(503, "processing_paused")
    refusal = upload_throttle.admit(membership.organisation_id, kind)
    if refusal is not None:
        raise Refused(429, "too_many_requests", str(refusal.retry_after_s))
    return user, membership.organisation


def _refusal(response: HttpResponse, refused: Refused) -> Status[Any]:
    if refused.status == 429:
        response["Retry-After"] = refused.message
        return Status(429, AddOriginalErrorOut(detail=refused.code))
    if refused.code == "processing_paused":
        response["Retry-After"] = str(PAUSED_RETRY_AFTER_S)
    return Status(refused.status, AddOriginalErrorOut(detail=refused.code, message=refused.message))


#: The recipe each kind of browser document must be on, and nothing else (ADR 0022: a tag is what
#: records which recipe produced a hash, so the endpoint names the ones it knows).
BROWSER_PIPELINES = {KIND_VIDEO: BROWSER_PIPELINE, KIND_PHOTOS: BROWSER_PHOTO_PIPELINE}


def _validated_shape(fingerprint: Fingerprint) -> None:
    """The checks that differ between a video and a photo collection (issue #116, ADR 0053).

    A video is sampled along a timeline and is capped by its positions; a collection has no rate
    and no duration at all (`reference_shape_matches_kind`) and is capped by its images. The two
    refusals are separate codes because the advice differs: shorten the video, or send fewer
    photographs.
    """
    if fingerprint.kind == KIND_PHOTOS:
        if fingerprint.frame_count > MAX_IMAGES:
            raise Refused(400, "too_many_images", f"at most {MAX_IMAGES} images per set")
        images = fingerprint.image_count
        if images is None or not 0 < images <= MAX_IMAGES:
            raise Refused(400, "invalid_fingerprint", f"image_count must be 1..{MAX_IMAGES}")
        # An image below the quality threshold leaves a gap in the ordinals rather than shifting
        # the rest (ADR 0020), so hashed positions never exceed the images that were read.
        if fingerprint.frame_count > images:
            raise Refused(400, "invalid_fingerprint", "more hashed images than image_count")
        return
    if fingerprint.fps not in SUPPORTED_FPS:
        raise Refused(400, "unsupported_fingerprint", f"fps must be one of {sorted(SUPPORTED_FPS)}")
    if fingerprint.frame_count > MAX_POSITIONS:
        raise Refused(400, "too_long", f"at most {MAX_POSITIONS} sampled positions")


def validated(payload: AddOriginalIn) -> Fingerprint:
    """The document, if it is one a customer's browser may send. Raises `Refused` otherwise."""
    if (
        not payload.rights_confirmed
        or payload.rights_text_version != CURRENT_VERSION
        or payload.language not in LANGUAGES
    ):
        raise Refused(400, "rights_confirmation_required")
    try:
        fingerprint = from_json(payload.fingerprint)
    except FingerprintFormatError as exc:
        raise Refused(400, "invalid_fingerprint", str(exc)) from exc
    if BROWSER_PIPELINES.get(fingerprint.kind) != fingerprint.pipeline:
        expected = ", ".join(f"{kind} on {tag}" for kind, tag in sorted(BROWSER_PIPELINES.items()))
        raise Refused(400, "unsupported_fingerprint", f"only {expected}")
    if not fingerprint.dihedral:
        raise Refused(400, "unsupported_fingerprint", "all eight orientations are required")
    if fingerprint.file_size > MAX_FILE_SIZE:
        raise Refused(400, "invalid_fingerprint", "file_size is out of range")
    if fingerprint.frame_count == 0:
        raise Refused(400, "invalid_fingerprint", "no frame reached the quality threshold")
    _validated_shape(fingerprint)
    if not fingerprint.creators:
        raise Refused(400, "invalid_fingerprint", "name the rights holder")
    # Printable text only, for the same reason on both fields: the title and the cast are the one
    # part of this document a customer types, and they are carried into places where a control
    # character is not ordinary text. The title reaches an email subject on the sandbox send path
    # (`marceline/notices/services.sandbox_send`), where Django refuses a multi-line header by
    # raising rather than by sending — a refusal at the boundary is the honest one.
    for text in (fingerprint.title, *fingerprint.creators):
        if not text.isprintable():
            raise Refused(400, "invalid_fingerprint", "the title and creator names must be text")
    return fingerprint


def normalised(name: str) -> str:
    return " ".join(name.split())


def creator_named(name: str) -> Creator:
    """The creator with exactly this name (case first as typed, then ignoring case), else a new
    one marked `needs_review`. Never an alias match, never a guess between several.

    `Creator.objects.usable()` is ADR 0038's single rule, and it decides which of several spellings
    a customer's name resolves to: a suppressed name, or a claim staff have not reviewed, never
    wins over one we may use. It deliberately does **not** decide whether to create a row — a name
    we may not use is still that person's record, and another spelling of it must not become a
    second one (docs/suppression.md, "a suppressed name is never indexed again"). Nothing here
    hunts, links or reports; the row only carries the customer's own cast for their own original,
    which is what a suppression keeps.
    """
    exact = Creator.objects.filter(name=name).first()
    if exact is not None:
        return exact
    folded = list(Creator.objects.filter(name__iexact=name).order_by("pk")[:2])
    allowed = set(
        Creator.objects.usable()
        .filter(pk__in=[creator.pk for creator in folded])
        .values_list("pk", flat=True)
    )
    usable = [creator for creator in folded if creator.pk in allowed]
    if len(usable) == 1:
        return usable[0]
    unusable = [creator for creator in folded if creator.pk not in allowed]
    if unusable:
        return unusable[0]
    creator, _ = Creator.objects.get_or_create(name=name, defaults={"needs_review": True})
    return creator


def customer_cast(names: Sequence[str]) -> tuple[Creator, list[Creator]]:
    """(rights holder, co-performers) for the names a customer typed, first name primary."""
    resolved = [creator_named(normalised(name)) for name in names]
    primary = resolved[0]
    seen = {primary.pk}
    others: list[Creator] = []
    for creator in resolved[1:]:
        if creator.pk not in seen:
            seen.add(creator.pk)
            others.append(creator)
    return primary, others


def _lock_file(sha256: str) -> None:
    """Serialise registrations of one file, so a double submit cannot import it twice."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", [FILE_LOCK_NAMESPACE, int(sha256[:7], 16)]
        )


@dataclass(frozen=True)
class ForeignClaim:
    """Why this upload touches material this organisation does not own — staff-only detail behind
    the generic 409 (`marceline.RefusedClaim`, `marceline/review/claims.py`)."""

    reason: RefusedClaim.Reason
    reference_ids: list[int]
    overlap_share: float | None = None

    def describe(self) -> str:
        if self.reason == RefusedClaim.Reason.HASH_OVERLAP:
            return f"hashes largely duplicate original(s) {self.reference_ids}"
        return f"same sha256 as original(s) {self.reference_ids}"


def _foreign_claim(fingerprint: Fingerprint, organisation: Organisation) -> ForeignClaim | None:
    """Why this upload touches material this organisation does not own; or None."""
    same_file = list(
        Reference.objects.filter(sha256=fingerprint.sha256).values_list("pk", flat=True)
    )
    if same_file:
        owned = set(
            owned_references(organisation).filter(pk__in=same_file).values_list("pk", flat=True)
        )
        foreign = sorted(set(same_file) - owned)
        if not foreign:
            return None
        has_owner = Reference.objects.filter(pk__in=foreign, ownership__isnull=False).exists()
        reason = (
            RefusedClaim.Reason.OWNED_BY_OTHER
            if has_owner
            else RefusedClaim.Reason.UNOWNED_EXISTING
        )
        return ForeignClaim(reason=reason, reference_ids=foreign)
    shares = overlap_shares(fingerprint)
    overlapping = sorted(
        reference_id for reference_id, share in shares.items() if share >= DUPLICATE_CLAIM_MIN_SHARE
    )
    if not overlapping:
        return None
    owned = set(
        owned_references(organisation).filter(pk__in=overlapping).values_list("pk", flat=True)
    )
    foreign = [reference_id for reference_id in overlapping if reference_id not in owned]
    if not foreign:
        return None
    return ForeignClaim(
        reason=RefusedClaim.Reason.HASH_OVERLAP,
        reference_ids=foreign,
        overlap_share=max(shares[reference_id] for reference_id in foreign),
    )


def _record_refused_claim(
    organisation: Organisation, sha256: str, claim: ForeignClaim, *, by: User
) -> None:
    """Staff-only record of a refused claim (issue #29 amendment). Never read by any customer
    endpoint; the customer keeps seeing only the generic 409."""
    RefusedClaim.objects.create(
        organisation=organisation,
        sha256=sha256,
        reason=claim.reason,
        overlap_share=claim.overlap_share,
        reference_ids=claim.reference_ids,
        requested_by=by,
    )


def _existing(fingerprint: Fingerprint, organisation: Organisation) -> OwnedOriginal | None:
    rows = list(owned_references(organisation).filter(sha256=fingerprint.sha256).order_by("pk"))
    if not rows:
        return None
    [original] = group_originals(rows, public_ids_for(organisation, [fingerprint.sha256]))
    return original


def _out(original: OwnedOriginal, *, created: bool) -> AddOriginalOut:
    findings = confirmed_findings_of([original]).get(original.pk, [])
    return AddOriginalOut(original=original_out(original, findings), created=created)


@router.post(
    "",
    response={
        200: AddOriginalOut,
        201: AddOriginalOut,
        202: AddOriginalAcceptedOut,
        400: AddOriginalErrorOut,
        403: AddOriginalErrorOut,
        409: AddOriginalErrorOut,
        413: AddOriginalErrorOut,
        429: AddOriginalErrorOut,
        503: AddOriginalErrorOut,
    },
)
@decorate_view(allow_large_body)
def add_original(
    request: HttpRequest, payload: AddOriginalIn, response: HttpResponse
) -> Status[AddOriginalOut | AddOriginalAcceptedOut | AddOriginalErrorOut]:
    """Add an original to the caller's organisation from fingerprints computed in the browser.

    201 created · 200 already in this account (nothing imported) · 202 received, being added
    (production: the outcome follows in `GET /app/fingerprints/submissions`, including a 409 or 400
    only development can decide) · 400 invalid document or missing rights declaration · 403 no
    organisation · 409 needs a human check (the file or its hashes are registered elsewhere; the
    answer never says where) · 413 body too large · 429 per-organisation limit, with `Retry-After`
    · 503 not available on this machine (`adding_originals_unavailable`) or paused for now
    (`processing_paused`, with `Retry-After`).
    """
    try:
        user, organisation = _organisation(request, upload_throttle.Kind.ORIGINAL)
        fingerprint = validated(payload)
        if into_outbox():
            item, _ = outbox.enqueue(
                kind=OutboxItem.Kind.ADD_ORIGINAL,
                organisation_id=organisation.pk,
                user_id=user.pk,
                payload=payload.model_dump(mode="json"),
                now=timezone.now(),
                title=fingerprint.title,
                dedupe_key=fingerprint.sha256,
            )
            return Status(202, AddOriginalAcceptedOut(submission=submission_out(item)))
        created, original = register_original(user, organisation, payload)
        return Status(201 if created else 200, _out(original, created=created))
    except Refused as refused:
        return _refusal(response, refused)


def register_original(
    user: User, organisation: Organisation, payload: AddOriginalIn
) -> tuple[bool, OwnedOriginal]:
    """Validate and register one original for `organisation`: (created, the original).

    The one registration path for customer uploads: this endpoint on development, and every outbox
    item production accepted (`marceline/customer_writes.py`). Raises `Refused` with the code the
    endpoint answers. Never rate-limited here: the endpoint (or production) counted the attempt.
    """
    return register_fingerprint(
        user,
        organisation,
        validated(payload),
        rights_text_version=payload.rights_text_version,
        language=payload.language,
    )


def register_fingerprint(
    user: User,
    organisation: Organisation,
    fingerprint: Fingerprint,
    *,
    rights_text_version: str,
    language: str,
    source_note: str = SOURCE_NOTE,
) -> tuple[bool, OwnedOriginal]:
    """Register one already-validated fingerprint document: (created, the original).

    Split out of `register_original` for cloud fingerprinting (issue #59, ADR 0044), which arrives
    at this point from the other end — the server computed the document itself from an uploaded
    file rather than receiving it from a browser. Everything after the document exists has to be
    identical for both, and the way to make sure of that is for there to be one function: the
    ownership row, the rights declaration and its confirmation, the cast mapping, the duplicate
    check, the generic 409 that names nobody and the staff-only `RefusedClaim` behind it.
    """
    # Checked again here and not only in `validated()`: a cloud upload's declaration was ticked
    # before the first byte was sent, and a version retired while the file was being hashed must
    # refuse the registration rather than be recorded as current.
    if rights_text_version != CURRENT_VERSION or language not in LANGUAGES:
        raise Refused(400, "rights_confirmation_required")
    foreign: ForeignClaim | None = None
    with transaction.atomic():
        _lock_file(fingerprint.sha256)
        foreign = _foreign_claim(fingerprint, organisation)
        if foreign is None:
            existing = _existing(fingerprint, organisation)
            if existing is not None:
                return False, existing
            creator, co_creators = customer_cast(fingerprint.creators)
            reference = import_fingerprint(
                creator=creator,
                fingerprint=fingerprint,
                creators=co_creators,
                source_note=source_note,
                mark_shared=False,
                source=SOURCE,
            )
            reference.uploaded_by = user
            reference.save(update_fields=["uploaded_by"])
            register(reference, organisation, via=Via.CUSTOMER, by=user)
            # The tick is also this work's self-declaration in the rights model (ADR 0034); the
            # confirmation row stays the upload's own audit record and points at it.
            work = rights_services.declare_at_upload(
                organisation,
                reference.sha256,
                text_version=rights_text_version,
                language=language,
                text=text_of(rights_text_version, language) or "",
                actor=rights_services.Actor.customer(user),
            )
            RightsConfirmation.objects.create(
                reference=reference,
                organisation=organisation,
                confirmed_by=user,
                text_version=rights_text_version,
                language=language,
                work_rights=work,
            )
    if foreign is not None:
        # Outside the atomic block on purpose: raising `Refused` below rolls nothing back that
        # matters here, but the review row must survive even though the claim itself is refused —
        # it is the only record staff ever see of why (`marceline.RefusedClaim`,
        # `marceline/review/claims.py`).
        logger.warning(
            "customer upload by organisation %s needs review: %s",
            organisation.pk,
            foreign.describe(),
        )
        _record_refused_claim(organisation, fingerprint.sha256, foreign, by=user)
        raise Refused(409, "claim_needs_review")
    logger.info("organisation %s added original %s in the app", organisation.pk, reference.pk)
    # The customer's `orig_…` id for this file; `register` created it with the owner.
    [original] = group_originals([reference], public_ids_for(organisation, [reference.sha256]))
    return True, original


def submission_out(item: OutboxItem) -> SubmissionOut:
    code = item.result_code if item.result_code in SUBMISSION_CODES else ""
    return SubmissionOut(
        id=item.public_id,
        title=item.title,
        status=SUBMISSION_STATUS[item.status],
        code=code,  # type: ignore[arg-type]
        original_id=item.original_id or None,
        received_at=item.received_at,
    )


@router.post(
    "/client-events",
    response={
        200: RecordedOut,
        403: AddOriginalErrorOut,
        429: AddOriginalErrorOut,
        503: AddOriginalErrorOut,
    },
)
def client_event(
    request: HttpRequest, payload: AppClientEventIn, response: HttpResponse
) -> Status[RecordedOut | AddOriginalErrorOut]:
    """Count a browser run that produced no fingerprints (issue #29 counters, input for #59).

    Only the decoder's words and the container type — never a file name, size or path. The answer
    carries no totals: how often *other* customers fail is not a customer's business.
    """
    try:
        user, organisation = _organisation(request, upload_throttle.Kind.CLIENT_EVENT)
    except Refused as refused:
        return _refusal(response, refused)
    if into_outbox():
        outbox.enqueue(
            kind=OutboxItem.Kind.CLIENT_EVENT,
            organisation_id=organisation.pk,
            user_id=user.pk,
            payload=payload.model_dump(mode="json"),
            now=timezone.now(),
        )
    else:
        record_client_event(user, payload)
    return Status(200, RecordedOut(recorded=True))


def record_client_event(user: User | None, payload: AppClientEventIn) -> None:
    ClientEvent.objects.create(
        kind=payload.kind,
        detail=payload.detail,
        container=payload.container,
        codec=payload.codec,
        user=user,
    )


@router.get("/submissions", response=list[SubmissionOut])
def submissions(request: HttpRequest) -> list[SubmissionOut]:
    """Originals this organisation sent that are being added, or were in the last days.

    Production accepts an upload first and adds it shortly after (202); this list is how the app
    shows that, and what became of it. Empty on development, where an upload is added at once, and
    for an account without an organisation.
    """
    membership = membership_of(request.user)
    if membership is None:
        return []
    rows = OutboxItem.objects.filter(
        organisation_id=membership.organisation_id, kind=OutboxItem.Kind.ADD_ORIGINAL
    ).order_by("-received_at", "-pk")[:MAX_SUBMISSIONS]
    return [submission_out(row) for row in rows]
