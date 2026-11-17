"""`POST /api/v1/originals`: hashes-only uploads from the browser fingerprinting client.

The original never leaves the creator's machine. The browser decodes and hashes it locally
(`frontend/src/lib/fingerprintClient.ts`) and sends exactly the document
`matching.fingerprint.to_json` produces - sha256, size, duration, sampling rate, title, creators
and PDQ hashes. No file, no frame, no thumbnail. Anything else is refused by `from_json`.

Imports go through `matching.references.import_fingerprint` and `matching.creators.set_creators`,
the same atomic path `add_reference` and `import_fingerprints` use, so every invariant (primary
creator always in `creators`, complete orientation sets) holds here too.

**One account, one registration per file.** The database only keeps a digest unique per pipeline,
so that the operator can re-fingerprint a library under a newer recipe and import the result
beside the old reference (issue #70, ADR 0022). That is an operator workflow on originals nobody
owns; a customer upload is a *claim*, and a second claim on a file somebody already registered is
exactly what must not slip through. This endpoint therefore refuses a sha256 that exists under any
pipeline, on its own, rather than leaning on a constraint that no longer says that.

**An upload is a claim, not proof of ownership.** The client runs on the creator's machine and
cannot be made tamper-proof, so the server does only what a server can do: it binds the row to the
uploading account (`Reference.uploaded_by`, audit only), refuses a file another account already
registered, and
refuses an upload whose hashes largely duplicate an original claimed by someone else. Nothing here
confirms a match or files a takedown - this endpoint writes one `Reference` and its hashes and
nothing else. See docs/adr/0019-browser-fingerprinting-client.md for what is left to human review.

**The owner is chosen, never implied** (issue #76, ADR 0031). `organisation_id` is required and may
be `null`: the uploader either names the organisation the original belongs to or says explicitly
that it stays unassigned (invisible to every customer). Who uploaded it decides nothing — a
reviewer registering material for a customer does not make it their own organisation's.
"""

from collections import defaultdict
from typing import Any, Literal

from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest
from ninja import Field, Router, Schema, Status

from accounts.models import Organisation
from catalog.models import Creator
from marceline.app.ownership import Via, register
from marceline.review.auth import staff_auth
from marceline.review.schemas import ConflictItemOut, ConflictOut, ErrorOut
from matching.creators import creator_names
from matching.fingerprint import Fingerprint, FingerprintFormatError, from_json
from matching.models import ClientEvent, Reference, ReferenceFrameHash
from matching.references import import_fingerprint, resolve_creators

router = Router(tags=["originals"], auth=staff_auth)

# Share of a new upload's dihedral-0 hashes that must already belong to one existing original
# before the upload counts as a claim on the same material.
DUPLICATE_CLAIM_MIN_SHARE = 0.5
# Hashes per `pdq__in` query; keeps the parameter list bounded for long originals.
HASH_QUERY_BATCH = 500
# Where the hashes were computed, stored in `Reference.client["source"]`.
SOURCE = "browser"


class CreatorOut(Schema):
    id: int
    name: str


class OrganisationChoiceOut(Schema):
    id: int
    name: str
    segment: str


class OriginalUploadIn(Schema):
    organisation_id: int | None = Field(
        description="Required: the organisation that owns this original, or null to leave it "
        "unassigned (invisible to customers). Never derived from the uploading account."
    )
    fingerprint: dict[str, Any] = Field(
        description="A `marceline-fingerprint` document (version 2) as produced by "
        "`matching.fingerprint.to_json`: hashes and metadata only, never file data."
    )
    creators: list[str] = Field(
        default_factory=list,
        description="Overrides the cast named in the document. The first name is the primary "
        "creator (rights holder), the rest co-performers (issue #64).",
    )
    source_note: str = Field(default="", description="Free text; no personal data.")


class OriginalUploadOut(Schema):
    reference_id: int
    title: str
    creator_names: list[str] = Field(description="Primary creator (rights holder) first.")
    frames: int = Field(description="Sampled positions stored (orientations counted once).")
    hashes: int = Field(description="Stored hash rows: positions times orientations.")
    pipeline: str
    uploaded_by: str = Field(description="Account this claim is bound to (audit only).")
    organisation: str | None = Field(description="Owning organisation; null = unassigned.")


class ClientEventIn(Schema):
    kind: Literal["codec_unsupported", "client_failed"]
    detail: str = Field(default="", description="Decoder message; never a file name or path.")
    container: str = ""
    codec: str = ""


class ClientEventOut(Schema):
    kind: str
    total: int = Field(description="Events of this kind recorded so far - the counter itself.")


def _account(request: HttpRequest) -> User:
    """The staff account behind this request; `staff_auth` has already vouched for it."""
    user = request.user
    if not isinstance(user, User):
        raise RuntimeError("authenticated user expected")
    return user


def overlap_shares(fingerprint: Fingerprint) -> dict[int, float]:
    """Per existing original, the share of this upload's dihedral-0 hashes it already stores.

    Exact byte equality, not a Hamming radius: this asks "is this the same hash set", which is
    what a duplicate claim looks like. A re-encode of the same video produces a different sha256
    but very similar hashes, and that is exactly the case worth a human's attention.
    """
    uploaded = sorted({frame.pdq for frame in fingerprint.frames if frame.dihedral == 0})
    if not uploaded:
        return {}
    seen: dict[int, set[bytes]] = defaultdict(set)
    for start in range(0, len(uploaded), HASH_QUERY_BATCH):
        batch = uploaded[start : start + HASH_QUERY_BATCH]
        rows = ReferenceFrameHash.objects.filter(dihedral=0, pdq__in=batch).values_list(
            "reference_id", "pdq"
        )
        for reference_id, pdq in rows:
            seen[reference_id].add(bytes(pdq))
    return {reference_id: len(values) / len(uploaded) for reference_id, values in seen.items()}


def duplicate_claims(fingerprint: Fingerprint, account: User) -> list[ConflictItemOut]:
    """Originals of *another* account that this upload largely duplicates.

    An original the same account uploaded is not a conflict: a creator may re-register their own
    material from a different master. An original imported by the operator's CLI (`uploaded_by`
    is null) counts as another party's, because nobody can vouch for the new claim automatically.
    """
    shares = {
        reference_id: share
        for reference_id, share in overlap_shares(fingerprint).items()
        if share >= DUPLICATE_CLAIM_MIN_SHARE
    }
    if not shares:
        return []
    conflicts = []
    for reference in (
        Reference.objects.filter(pk__in=sorted(shares)).select_related("uploaded_by").order_by("pk")
    ):
        if reference.uploaded_by_id == account.pk:
            continue
        owner = (
            reference.uploaded_by.get_username()
            if reference.uploaded_by is not None
            else "an operator import"
        )
        conflicts.append(
            ConflictItemOut(
                kind="reference",
                id=reference.pk,
                status="claimed",
                reason=f"{shares[reference.pk]:.0%} of these frame hashes already belong to "
                f"original {reference.pk}, claimed by {owner}",
            )
        )
    return conflicts


@router.get("/creators", response=list[CreatorOut])
def creators(request: HttpRequest) -> list[CreatorOut]:
    """Creators to pick from when registering an original; the first picked is the rights holder."""
    # `usable()` is ADR 0038's single rule and it applies to a picker too: a suppressed name is not
    # offered to staff either (offering it is how it gets linked to an original again), and a name a
    # customer typed that nobody has reviewed is a claim, not a creator (issue #29). Staff mark such
    # a name reviewed in the admin, and it appears here. This docstring is the endpoint's OpenAPI
    # description, so the reasoning stays in a comment.
    return [
        CreatorOut(id=creator.pk, name=creator.name)
        for creator in Creator.objects.usable().order_by("name")
    ]


@router.get("/organisations", response=list[OrganisationChoiceOut])
def organisations(request: HttpRequest) -> list[OrganisationChoiceOut]:
    """Organisations an original can be registered for; the uploader must pick one or none."""
    return [
        OrganisationChoiceOut(id=row.pk, name=row.name, segment=row.segment)
        for row in Organisation.objects.order_by("name")
    ]


@router.post("", response={200: OriginalUploadOut, 400: ErrorOut, 409: ConflictOut})
def upload_original(
    request: HttpRequest, payload: OriginalUploadIn
) -> Status[OriginalUploadOut | ErrorOut | ConflictOut]:
    """Register an original from hashes computed in the browser.

    Refuses (400) a document `from_json` will not validate, an unknown organisation and an unknown
    or ambiguous creator;
    refuses (409) a file already imported and an upload that duplicates another account's claim.
    Shared-frame flags are not recomputed here - `match_references` does that at the start of
    every run - and no match, leak or takedown is created or confirmed by this call.
    """
    account = _account(request)
    organisation = None
    if payload.organisation_id is not None:
        organisation = Organisation.objects.filter(pk=payload.organisation_id).first()
        if organisation is None:
            return Status(400, ErrorOut(detail="Unknown organisation"))
    try:
        fingerprint = from_json(payload.fingerprint)
    except FingerprintFormatError as exc:
        return Status(400, ErrorOut(detail=f"Malformed fingerprint: {exc}"))
    names = payload.creators or list(fingerprint.creators)
    if not names:
        return Status(400, ErrorOut(detail="No creator: name at least the rights holder"))
    try:
        creator, co_creators = resolve_creators(names)
    except ValueError as exc:
        return Status(400, ErrorOut(detail=str(exc)))
    with transaction.atomic():
        # Any pipeline: for a claim, "this file is already registered" is the question, not
        # "which recipe hashed it" (see the module docstring).
        existing = Reference.objects.filter(sha256=fingerprint.sha256).order_by("pk").first()
        if existing is not None:
            return Status(
                409,
                ConflictOut(
                    detail=f"This file is already registered as original {existing.pk}",
                    conflicts=[
                        ConflictItemOut(
                            kind="reference",
                            id=existing.pk,
                            status="imported",
                            reason="same sha256",
                        )
                    ],
                ),
            )
        conflicts = duplicate_claims(fingerprint, account)
        if conflicts:
            return Status(
                409,
                ConflictOut(
                    detail="These hashes are already claimed by another account; a human has to "
                    "decide who holds the rights before this original is registered",
                    conflicts=conflicts,
                ),
            )
        reference = import_fingerprint(
            creator=creator,
            fingerprint=fingerprint,
            creators=co_creators,
            source_note=payload.source_note,
            mark_shared=False,
            source=SOURCE,
        )
        reference.uploaded_by = account
        reference.save(update_fields=["uploaded_by"])
        register(reference, organisation, via=Via.UPLOAD, by=account)
    return Status(
        200,
        OriginalUploadOut(
            reference_id=reference.pk,
            title=reference.title,
            creator_names=creator_names(reference),
            frames=fingerprint.frame_count,
            hashes=len(fingerprint.frames),
            pipeline=reference.pipeline,
            uploaded_by=account.get_username(),
            organisation=organisation.name if organisation is not None else None,
        ),
    )


@router.post("/client-events", response=ClientEventOut)
def client_event(request: HttpRequest, payload: ClientEventIn) -> ClientEventOut:
    """Record why a browser run produced no hashes (issue #29 counters, input for #59).

    `codec_unsupported` means the browser refused to decode the file at all, `client_failed`
    every other local failure. Deliberately no file name, size or path - only the codec strings
    the decoder reported, so the counts say whether cloud fingerprinting is worth building.
    """
    ClientEvent.objects.create(
        kind=payload.kind,
        detail=payload.detail[: ClientEvent.DETAIL_LENGTH],
        container=payload.container[: ClientEvent.SHORT_LENGTH],
        codec=payload.codec[: ClientEvent.SHORT_LENGTH],
        user=_account(request),
    )
    return ClientEventOut(
        kind=payload.kind, total=ClientEvent.objects.filter(kind=payload.kind).count()
    )
