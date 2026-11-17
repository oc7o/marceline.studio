"""The export a customer is offered before their data is deleted (ADR 0045, issue #85).

One JSON document, no new dependency, three parts:

- **their own API documents**, rendered through `marceline.export.render` — the same function
  ADR 0028's signed bundle uses, which resolves the customer path and calls the very view a
  signed-in member would reach. So `originals`, `findings`, `cases`, `removals`, `rights` and every
  `cases/{id}` are byte-for-byte what the customer was being shown: same ownership scoping, same
  confirmed-only and safety filters, the page addresses they were already shown, and nothing they
  were not. There is no second serializer that could show them more than the product did;
- **their originals' fingerprints**: per reference the recipe, the digest and each frame's
  `frame_index`, `timestamp_s`, `dihedral`, `quality` and PDQ hash as hex — the frame shape of
  `matching.fingerprint.to_json`. It is deliberately **not** a re-importable client document and
  says so in its own `note`: `file_size` and `image_count` are not stored on a `Reference`, and
  inventing them would be a lie in a file a customer keeps;
- **their claimed platform accounts** (ADR 0055): per claim the platform, the handle, the address
  they gave us, where it stands, how and when it was settled and its whole event log. No code
  travels: a settled claim has none and a live one is in the app in front of them;
- **the export and deletion requests they made** (ADR 0056): what they asked for, when, where it
  stands and what we noted while answering — the record only we hold of an Art. 12(3) exchange;
- **their notice drafts** (ADR 0035): each draft's snapshot fields, its reported URL snapshots, the
  exact confirmation texts they ticked and its event log — their own audit trail of what they
  asserted, and the part of a deletion hardest to reconstruct.

Hashes, ids, dates and codes. No frame, no thumbnail, no poster; every string about a page in here
is one the customer was already shown.

Staff-triggered from the command line (`manage.py export_customer_data`), and customer-triggered
at `GET /api/v1/app/export` (`marceline/app/export.py`, ADR 0045 amendment 2026-09-18): both call
this module's `export_document(organisation)` — there is no second implementation — and both stamp
`OrganisationEnding.exported_at` through `stamp_exported_at` when a pending ending exists. The
customer route stays development-only (`ROLE_ONLY_PREFIXES["dev"]`, as rights and notices are)
until someone decides how a large document travels through the signed cache.
"""

import json
from datetime import datetime
from typing import Any

from django.utils import timezone

from accounts.models import Organisation
from marceline.app.ownership import owned_originals
from marceline.deletion.models import CustomerDataRequest, OrganisationEnding
from marceline.export import (
    APP_PREFIX,
    LIST_DOCUMENTS,
    ORIGINAL_DETAIL_FAMILIES,
    member_of,
    render,
)
from marceline.notices.models import NoticeDraft
from marceline.rights.models import AccountControlChallenge
from matching.models import Reference, ReferenceFrameHash

FORMAT = "marceline.customer-export"
FORMAT_VERSION = 1

#: Why the fingerprint section is not a client document (`matching.fingerprint.FORMAT`).
FINGERPRINT_NOTE = (
    "Stored hashes, in the frame shape of the Marceline client's fingerprint document. Not a "
    "client document: a stored original carries no file size or image count, so those fields are "
    "absent rather than invented."
)


def _frames(reference: Reference) -> list[dict[str, Any]]:
    rows = ReferenceFrameHash.objects.filter(reference=reference, dihedral=0).order_by(
        "frame_index"
    )
    return [
        {
            "frame_index": row.frame_index,
            "timestamp_s": row.timestamp_s,
            "quality": row.quality,
            "pdq": bytes(row.pdq).hex(),
        }
        for row in rows
    ]


def fingerprints_of(reference: Reference) -> dict[str, Any]:
    """One stored original: its metadata and its unflipped frame hashes.

    Only the unflipped orientation (`dihedral=0`) is exported: the other seven are computed from
    it, and shipping eight copies of every hash makes the file eight times the size for no
    information.
    """
    return {
        "reference_id": reference.pk,
        "kind": reference.kind,
        "title": reference.title,
        "sha256": reference.sha256,
        "pipeline": reference.pipeline,
        "embedding_pipeline": reference.embedding_pipeline,
        "frame_fps": reference.frame_fps,
        "duration_s": reference.duration_s,
        "created_at": reference.created_at.isoformat(),
        "source_note": reference.source_note,
        "creators": sorted(row.name for row in reference.creators.all()),
        "frames": _frames(reference),
    }


def notice_drafts_of(organisation: Organisation) -> list[dict[str, Any]]:
    """Every notice draft of this organisation, with its findings, confirmations and events."""
    drafts = (
        NoticeDraft.objects.filter(organisation=organisation)
        .prefetch_related("findings", "confirmations", "events")
        .order_by("pk")
    )
    return [
        {
            "id": draft.pk,
            "status": draft.status,
            "language": draft.language,
            "rendered_variant": draft.rendered_variant,
            "recipient_hostname": draft.recipient_hostname,
            "channel_kind": draft.channel_kind,
            "sha256": draft.sha256,
            "original_id": draft.original_public_id,
            "work_title": draft.work_title,
            "created_at": draft.created_at.isoformat(),
            "approved_at": draft.approved_at.isoformat() if draft.approved_at else None,
            "approved_text": draft.approved_text,
            "approved_text_sha256": draft.approved_text_sha256,
            "marked_sent_at": (draft.marked_sent_at.isoformat() if draft.marked_sent_at else None),
            "responded_at": draft.responded_at.isoformat() if draft.responded_at else None,
            "outcome": draft.outcome,
            "withdrawn_at": draft.withdrawn_at.isoformat() if draft.withdrawn_at else None,
            "findings": [
                {
                    "url": finding.url_snapshot,
                    "availability": finding.availability_snapshot,
                    "extent": finding.extent_snapshot,
                    "included": finding.included,
                    "exception_confirmed": finding.exception_confirmed,
                }
                for finding in draft.findings.all()
            ],
            "confirmations": [
                {
                    "kind": confirmation.kind,
                    "version": confirmation.version,
                    "language": confirmation.language,
                    "text": confirmation.text,
                    "confirmed_at": confirmation.confirmed_at.isoformat(),
                }
                for confirmation in draft.confirmations.all()
            ],
            "events": [
                {"kind": event.kind, "at": event.at.isoformat(), "detail": event.detail}
                for event in draft.events.all()
            ],
        }
        for draft in drafts
    ]


def customer_documents(organisation: Organisation) -> dict[str, Any]:
    """The customer's own API documents, rendered exactly as the live endpoints answer them."""
    user = member_of(organisation)
    if user is None:
        return {}
    documents: dict[str, Any] = {}
    keys = [*LIST_DOCUMENTS]
    # Only the families keyed by an original's public id: since ADR 0046 `notices/{id}` is keyed by
    # a *draft's* id, and a draft's contents are already in this document in full
    # (`notice_drafts_of`), so asking for it here would only build keys that answer 404.
    keys += [
        f"{family}/{original.public_id}"
        for family in ORIGINAL_DETAIL_FAMILIES
        for original in owned_originals(organisation)
    ]
    for key in keys:
        status, _content_type, body = render(f"{APP_PREFIX}{key}", user)
        if status == 200:
            documents[key] = json.loads(body)
    return documents


def account_challenges_of(organisation: Organisation) -> list[dict[str, Any]]:
    """Every platform account this organisation claimed (ADR 0055), with its event log.

    The **code is deliberately absent**: a settled claim no longer has one, a live one is on the
    screen the customer is reading anyway, and a document they keep on a disk should not carry a
    credential-shaped string at all. Nothing here is an identity document, a document number or a
    date of birth — this flow never stores one.
    """
    challenges = (
        AccountControlChallenge.objects.filter(organisation=organisation)
        .prefetch_related("events")
        .order_by("pk")
    )
    return [
        {
            "id": challenge.public_id,
            "platform": challenge.platform,
            "handle": challenge.handle,
            "profile_url": challenge.profile_url,
            "state": challenge.state,
            "codes_issued": challenge.issue_count,
            "code_expires_at": (
                challenge.code_expires_at.isoformat() if challenge.code_expires_at else None
            ),
            "placed_at": challenge.placed_at.isoformat() if challenge.placed_at else None,
            "method": challenge.method,
            "decided_at": challenge.decided_at.isoformat() if challenge.decided_at else None,
            "note": challenge.note,
            "created_at": challenge.created_at.isoformat(),
            "events": [
                {
                    "at": event.at.isoformat(),
                    "kind": event.kind,
                    "method": event.method,
                    "actor_kind": event.actor_kind,
                    "note": event.note,
                }
                for event in challenge.events.all()
            ],
        }
        for challenge in challenges
    ]


def data_requests_of(organisation: Organisation) -> list[dict[str, Any]]:
    """Every export or deletion this organisation asked for (ADR 0056), oldest first.

    Their own record of having asked, which is the part of an Art. 12(3) exchange only we hold. The
    staff note is deliberately in it: it is what we wrote about their request, so it is theirs to
    read. It carries no personal data by rule (`CustomerDataRequest.staff_note`).
    """
    return [
        {
            "kind": row.kind,
            "status": row.status,
            "requested_at": row.requested_at.isoformat(),
            "settled_at": row.settled_at.isoformat() if row.settled_at else None,
            "staff_note": row.staff_note,
        }
        for row in CustomerDataRequest.objects.filter(organisation=organisation).order_by("pk")
    ]


def export_document(organisation: Organisation, *, now: datetime | None = None) -> dict[str, Any]:
    """Everything this organisation registered and everything we recorded for it."""
    moment = now or timezone.now()
    references = (
        Reference.objects.filter(ownership__organisation=organisation)
        .prefetch_related("creators")
        .order_by("pk")
    )
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "exported_at": moment.isoformat(),
        "organisation": {
            "id": organisation.public_id,
            "name": organisation.name,
            "segment": organisation.segment,
            "created_at": organisation.created_at.isoformat(),
        },
        "documents": customer_documents(organisation),
        "fingerprints": {
            "note": FINGERPRINT_NOTE,
            "originals": [fingerprints_of(reference) for reference in references],
        },
        "notice_drafts": notice_drafts_of(organisation),
        "account_verification": account_challenges_of(organisation),
        "data_requests": data_requests_of(organisation),
    }


def stamp_exported_at(organisation: Organisation, moment: datetime) -> bool:
    """Record that this organisation's export was just written, if a pending ending exists.

    `OrganisationEnding.exported_at` answers "was the promised export actually offered" — read by
    `end_pilot --list` and the admin. An organisation with no ending mark yet has nothing to stamp:
    a customer may download their own data at any time, which is not the same event as the export
    the pilot's end promises. Returns whether a row was stamped.
    """
    return bool(
        OrganisationEnding.objects.filter(organisation=organisation).update(exported_at=moment)
    )
