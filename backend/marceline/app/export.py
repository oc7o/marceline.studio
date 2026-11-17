"""The customer's own data export (ADR 0045 amendment 2026-09-18, issue #85):
`GET /api/v1/app/export`.

The one place this plugs in, exactly as the ADR named it: `export_document(organisation)` —
`marceline/deletion/export.py`, the same function `manage.py export_customer_data` writes to a
file — rendered here as a JSON attachment instead. There is no second serializer: everything the
document contains is either the customer's own API documents (rendered through the customer views
themselves) or hashes/ids the customer was already shown, so this view adds no staff-only field.

Own data only, by construction: the organisation comes from the signed-in session
(`organisation_of`), never from a parameter, exactly like every other customer read.

**Rate-limited** with the same per-organisation throttle customer writes use
(`marceline/app/upload_throttle.py`, `Kind.EXPORT`, one per rolling hour) — an export reads every
customer document, every reference frame hash and every notice draft in one call, as heavy as an
upload, and a second request while the first is still fresh would only repeat it.

**Audited, without storing the document itself**: one log line per export (organisation, the
account that asked, byte size, sha256) and, when this organisation's pilot has already been marked
ended, the same `OrganisationEnding.exported_at` stamp the staff command writes
(`marceline.deletion.export.stamp_exported_at`) — the record ADR 0045 already keeps of "was the
promised export actually offered". The document itself is never persisted here or anywhere else.

**Development only** (`public_surface.ROLE_ONLY_PREFIXES["dev"]`, like rights proofs and notices):
production holds no originals, rights records or notice drafts beyond its signed customer-cache
bundle, so there is nothing this route could add there — it answers the same 404 as an unknown
path.
"""

import hashlib
import json
import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Router, Status

from marceline.app import upload_throttle
from marceline.app.auth import customer_auth
from marceline.app.ownership import organisation_of
from marceline.app.schemas import AppErrorOut
from marceline.deletion.export import export_document, stamp_exported_at

router = Router(tags=["app"], auth=customer_auth)

logger = logging.getLogger("marceline.app.export")

NOT_FOUND = "Organisation not found"


def export_available() -> bool:
    """Whether this machine answers the export at all: development only, never production
    (`account.py`'s `can_export_data`; the route itself is dev-only too — `public_surface`)."""
    return settings.MARCELINE_ROLE != "prod"


EXPORT_OPENAPI = {
    "responses": {
        "200": {
            "description": "Everything this organisation registered and everything we recorded "
            "for it, as one JSON document (attachment) — the same document "
            "`manage.py export_customer_data` writes to a file.",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    }
}


@router.get("", response={404: AppErrorOut, 429: AppErrorOut}, openapi_extra=EXPORT_OPENAPI)
def export(request: HttpRequest, response: HttpResponse) -> HttpResponse | Status[AppErrorOut]:
    """Download this organisation's own export: their own API documents, their originals'
    fingerprints, and their notice drafts — `export_document`, streamed as a file.

    404 for a signed-in account with no organisation (there is nothing to export — the same rule
    `GET /app/rights` uses). 429, with `Retry-After`, once more than one export has been requested
    in the rolling hour.
    """
    organisation = organisation_of(request.user)
    if organisation is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    refusal = upload_throttle.admit(organisation.pk, upload_throttle.Kind.EXPORT)
    if refusal is not None:
        response["Retry-After"] = str(refusal.retry_after_s)
        return Status(429, AppErrorOut(detail="too_many_requests"))
    moment = timezone.now()
    document = export_document(organisation, now=moment)
    body = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    stamp_exported_at(organisation, moment)
    logger.info(
        "organisation %s exported their own data (user %s, %d bytes, sha256 %s)",
        organisation.pk,
        request.user.pk,
        len(body),
        digest,
    )
    filename = f"marceline-export-{moment:%Y-%m-%d}.json"
    download = HttpResponse(body, content_type="application/json")
    download["Content-Disposition"] = f'attachment; filename="{filename}"'
    download["Cache-Control"] = "private, no-store"
    download["X-Content-Type-Options"] = "nosniff"
    return download
