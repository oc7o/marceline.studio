"""The creator's own data requests: `/api/v1/app/data-requests` (ADR 0056, issue #133).

Two requests and nothing else: *send me a copy of my data* (Art. 15, Art. 20 GDPR) and *delete my
data / end my pilot* (Art. 17). This is the half of #133 that the public host was missing — the
export route is development-only (`public_surface.ROLE_ONLY_PREFIXES`) because production holds no
originals, rights records or drafts of its own, and there was no deletion route anywhere at all. So
an invited creator who can only reach the public host could exercise neither right from inside the
product.

**What a request does not do.** It does not export and it does not delete. ADR 0045's rule is the
reason and it is not being bent here: the clock for a deletion is a date a human sets
(`manage.py end_pilot`) and the export is produced by `manage.py export_customer_data` and handed
over out of band for now. What the creator gets is the request itself — recorded, dated, visible to
staff in the admin, withdrawable while it is open, and carrying the one-month deadline of
Art. 12(3) GDPR and the address it is answered from. The development-only direct download stays
exactly where it is (`marceline/app/export.py`); on a machine that offers it, both exist.

- `GET ""` — this organisation's requests, the contact address, the deadline and the word a deletion
  request has to echo. A pure read, so production serves it from the signed bundle (ADR 0028).
- `POST "/export"` / `POST "/deletion"` — open one. A deletion needs the typed confirmation.
- `POST "/export/withdraw"` / `POST "/deletion/withdraw"` — take the open one back.

The paths name the kind rather than a request id on purpose: at most one request per kind is ever
open, so "withdraw my open deletion request" is unambiguous and there is no id a caller could point
at another organisation's row with. Every read and write is scoped by `organisation_of`, so another
organisation's request is not "forbidden" — it is invisible, and a withdraw finds nothing.

With `CUSTOMER_DATA_REQUESTS_ENABLED` off every route answers one plain 404, like the verification
routes with their own switch off.
"""

from datetime import datetime
from typing import Any, Literal, cast

from django.contrib.auth.models import User
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema, Status
from pydantic import Field

from accounts.models import Organisation
from delivery import outbox
from delivery.models import OutboxItem
from marceline.app import upload_throttle
from marceline.app.auth import customer_auth
from marceline.app.ownership import organisation_of
from marceline.app.rights_status import into_outbox, writes_available
from marceline.app.schemas import AppErrorOut
from marceline.deletion import requests as data_requests
from marceline.deletion.models import CustomerDataRequest

router = Router(tags=["app"], auth=customer_auth)

#: What every route answers while the feature is off, and what a withdraw with nothing open gets.
UNAVAILABLE = "Not found"

KindCode = Literal["export", "deletion"]
StatusCode = Literal["requested", "in_progress", "done", "withdrawn"]

assert set(CustomerDataRequest.Kind.values) == set(KindCode.__args__)  # type: ignore[attr-defined]
assert set(CustomerDataRequest.Status.values) == set(StatusCode.__args__)  # type: ignore[attr-defined]


class DataRequestOut(Schema):
    kind: KindCode
    status: StatusCode = Field(
        description="requested: we have it. in_progress: somebody is working on it. done: it was "
        "answered. withdrawn: you took it back."
    )
    requested_at: datetime
    due_at: datetime = Field(
        description="When the answer is due at the latest (Art. 12(3) GDPR, one month)."
    )
    settled_at: datetime | None = Field(
        description="When it was answered or withdrawn. Null while it is open."
    )
    open: bool


class DataRequestsOut(Schema):
    """Everything the screen needs, including the words it must not invent itself."""

    contact_email: str = Field(
        description="Where a request is answered from, and where one can also simply be written."
    )
    deletion_phrase: str = Field(
        description="The word a deletion request has to echo, the same in every language."
    )
    requests: list[DataRequestOut]


class DeletionRequestIn(Schema):
    confirm: str = Field(
        max_length=64,
        description="Must equal `deletion_phrase` exactly. Deleting cannot be undone, so the "
        "request is not taken from a single press of a button.",
    )


class DataRequestAcceptedOut(Schema):
    """Production's answer to a write it cannot apply itself (ADR 0028 write path): accepted into
    the outbox, applied by development shortly. The screen says so and never says "done"."""

    pending: bool = Field(default=True, description="Accepted; not yet applied.")


def _out(row: CustomerDataRequest) -> DataRequestOut:
    return DataRequestOut(
        kind=cast(KindCode, row.kind),
        status=cast(StatusCode, row.status),
        requested_at=row.requested_at,
        due_at=row.requested_at + data_requests.DEADLINE,
        settled_at=row.settled_at,
        open=row.open,
    )


def status_out(organisation: Organisation) -> DataRequestsOut:
    return DataRequestsOut(
        contact_email=data_requests.CONTACT_EMAIL,
        deletion_phrase=data_requests.DELETION_PHRASE,
        requests=[_out(row) for row in data_requests.requests_for(organisation)],
    )


def _organisation(request: HttpRequest) -> tuple[Organisation | None, User]:
    user = cast(User, request.user)
    return organisation_of(user), user


def _gate_or_enqueue(
    request: HttpRequest, organisation: Organisation, kind: str, payload: dict[str, Any]
) -> Status[DataRequestAcceptedOut] | Status[AppErrorOut] | None:
    """Production takes the write down instead of applying it; `None` on development.

    The same shape as `rights_status._gate_or_enqueue` and for the same reason: production's own
    tables hold no requests, so the row is written where it can be read — by the staff member who
    has to answer it. `dedupe_key` is the request kind, so a double press is one write.
    """
    if not into_outbox():
        return None
    if not writes_available():
        return Status(503, AppErrorOut(detail="data_request_write_unavailable"))
    now = timezone.now()
    if outbox.paused(now):
        return Status(503, AppErrorOut(detail="processing_paused"))
    user = cast(User, request.user)
    if (
        upload_throttle.admit(organisation.pk, upload_throttle.Kind.RIGHTS_WRITE, now=now)
        is not None
    ):
        return Status(429, AppErrorOut(detail="too_many_requests"))
    outbox.enqueue(
        kind=kind,
        organisation_id=organisation.pk,
        user_id=user.pk,
        payload=payload,
        now=now,
        dedupe_key=str(payload.get("kind", "")),
    )
    return Status(202, DataRequestAcceptedOut())


WRITE_RESPONSES = {
    200: DataRequestsOut,
    202: DataRequestAcceptedOut,
    404: AppErrorOut,
    409: AppErrorOut,
    429: AppErrorOut,
    503: AppErrorOut,
}


@router.get("", response={200: DataRequestsOut, 404: AppErrorOut})
def data_request_status(request: HttpRequest) -> Status[DataRequestsOut] | Status[AppErrorOut]:
    """This organisation's own requests, with the deadline and the address answering them."""
    if not data_requests.available():
        return Status(404, AppErrorOut(detail=UNAVAILABLE))
    organisation, _ = _organisation(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail="Organisation not found"))
    return Status(200, status_out(organisation))


def _open(
    request: HttpRequest, kind: str, *, confirm: str = ""
) -> Status[DataRequestsOut] | Status[DataRequestAcceptedOut] | Status[AppErrorOut]:
    organisation, user = _organisation(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail=UNAVAILABLE))
    # The typed word travels with the write, so development can judge a deletion request for itself
    # rather than trusting that the public host checked it (`marceline/customer_writes.py`).
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.DATA_REQUEST_OPEN,
        {"kind": kind, "confirm": confirm},
    )
    if gate is not None:
        return gate
    try:
        data_requests.open_request(organisation, kind, by=user)
    except data_requests.DataRequestError as error:
        return Status(409, AppErrorOut(detail=error.code))
    return Status(200, status_out(organisation))


def _withdraw(
    request: HttpRequest, kind: str
) -> Status[DataRequestsOut] | Status[DataRequestAcceptedOut] | Status[AppErrorOut]:
    organisation, _ = _organisation(request)
    if organisation is None:
        return Status(404, AppErrorOut(detail=UNAVAILABLE))
    gate = _gate_or_enqueue(
        request, organisation, OutboxItem.Kind.DATA_REQUEST_WITHDRAW, {"kind": kind}
    )
    if gate is not None:
        return gate
    try:
        data_requests.withdraw(organisation, kind)
    except data_requests.DataRequestError as error:
        return Status(409, AppErrorOut(detail=error.code))
    return Status(200, status_out(organisation))


@router.post("/export", response=WRITE_RESPONSES)
def request_export(
    request: HttpRequest,
) -> Status[DataRequestsOut] | Status[DataRequestAcceptedOut] | Status[AppErrorOut]:
    """Ask for a copy of everything this organisation's account holds (Art. 15, Art. 20).

    409 `already_open` when one is already open, which the screen reads as "we have it".
    202 on production: accepted into the outbox, recorded on development a minute later.
    """
    if not data_requests.available():
        return Status(404, AppErrorOut(detail=UNAVAILABLE))
    return _open(request, CustomerDataRequest.Kind.EXPORT)


@router.post("/deletion", response=WRITE_RESPONSES)
def request_deletion(
    request: HttpRequest, payload: DeletionRequestIn
) -> Status[DataRequestsOut] | Status[DataRequestAcceptedOut] | Status[AppErrorOut]:
    """Ask for this organisation's data to be deleted and the pilot to end (Art. 17).

    The typed word is checked **here and again on development**, because the write may travel
    through the outbox and the second machine must not take a deletion request on trust. 409
    `confirmation_mismatch` when it does not match, `already_open` when one is already open.

    It still deletes nothing: a staff member sets the end date (ADR 0045), the retention window runs
    from there, and until then the creator can withdraw the request.
    """
    if not data_requests.available():
        return Status(404, AppErrorOut(detail=UNAVAILABLE))
    confirm = payload.confirm.strip()
    if confirm != data_requests.DELETION_PHRASE:
        return Status(409, AppErrorOut(detail="confirmation_mismatch"))
    return _open(request, CustomerDataRequest.Kind.DELETION, confirm=confirm)


@router.post("/export/withdraw", response=WRITE_RESPONSES)
def withdraw_export(
    request: HttpRequest,
) -> Status[DataRequestsOut] | Status[DataRequestAcceptedOut] | Status[AppErrorOut]:
    """Take back the open export request. 409 `not_open` when there is none."""
    if not data_requests.available():
        return Status(404, AppErrorOut(detail=UNAVAILABLE))
    return _withdraw(request, CustomerDataRequest.Kind.EXPORT)


@router.post("/deletion/withdraw", response=WRITE_RESPONSES)
def withdraw_deletion(
    request: HttpRequest,
) -> Status[DataRequestsOut] | Status[DataRequestAcceptedOut] | Status[AppErrorOut]:
    """Take back the open deletion request while it is still open. 409 `not_open` when there is
    none — which is also what another organisation's open request gets, because this only ever
    looks at the caller's own."""
    if not data_requests.available():
        return Status(404, AppErrorOut(detail=UNAVAILABLE))
    return _withdraw(request, CustomerDataRequest.Kind.DELETION)
