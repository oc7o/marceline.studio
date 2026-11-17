"""Notice drafts (issue #33, ADR 0035): `/api/v1/app/notices`.

"Prepare notice draft" per record, per provider: shows blockers in codes, lets the customer pick
which confirmed findings and which suggested recipient/channel to use, walks through the required
confirmations, freezes the exact approved text, and records what the customer did next
("mark as sent by me", the provider's response, withdraw, correct). Marceline never sends anything
to a real recipient here — the sandbox send path is the one exception, and only for the operator's
sandbox organisation to a `NOTICE_SANDBOX_RECIPIENTS` test mailbox.

The one read that is not JSON is `GET /{notice_id}/export/{fmt}` (ADR 0035 amendment
2026-09-18): the approved text as a file the customer keeps — plain text, an unsent `.eml` for
their own mail program, or the evidence annex. It formats bytes and records one `exported` event;
it transmits nothing.

Same rules as the rest of the customer API: scoped by `ownership`, public ids only (`notice_`,
plus the existing `orig_`/`page_`), a malformed, foreign or unknown id is one byte-identical 404,
CSRF on every write, no media, no confidence, no band, no hash. **A safety-flagged page is never
mentioned** (ADR 0011, ADR 0018): a draft keeps each reported URL in a snapshot of its own, so a
draft whose page stopped being visible is answered with the same 404 as an unknown id and is left
out of the list (`services.visible`) — the record itself stays, for staff.

**Reads and writes reach production** (ADR 0046). The four reads are answered there from the
signed cache bundle (`marceline.export` renders them, `marceline.customer_cache` serves the three
JSON ones; the download is served by `_export_from_cache` below, because its audit event is a
write). Every write except the sandbox send goes through the production outbox: validated with
what production can check by itself (session, CSRF, the rate limit, the schema), stored as a
`delivery.OutboxItem` and answered **202** with `NoticeWriteAcceptedOut` — development pulls it
and applies it through the very functions this module calls, re-running the notice gate, the
safety filter and the state machine against today's data (`marceline/customer_writes.py`). The
customer follows it in `GET /app/notices/submissions`, a production-local read. The **sandbox
send stays development-only** (`public_surface.ROLE_ONLY_PREFIXES["dev"]`, which is why its path
puts `sandbox-send` in front of the id — the allowlist is a prefix allowlist): it is the only
code path in the product that may hand a draft to a mail transport, and it must not exist on the
public host at all.
"""

from datetime import date, datetime
from typing import Any, Literal, cast

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Router, Schema, Status
from pydantic import Field

from accounts.models import Organisation
from catalog.hosting.directory import ChannelView, ProviderRole, provider_roles
from catalog.hosting.services import normalize_hostname
from core import public_ids
from delivery import outbox, store
from delivery.models import OutboxItem
from marceline import customer_cache
from marceline.app import upload_throttle
from marceline.app.auth import customer_auth
from marceline.app.originals import confirmed_findings_of, extent_of
from marceline.app.ownership import membership_of, organisation_of, owned_original
from marceline.app.schemas import AppErrorOut
from marceline.notices import declarations, export, services
from marceline.notices.gate import file_matches, finding_gate_blockers
from marceline.notices.models import (
    NOTICE_ID_PREFIX,
    ConfirmationKind,
    NoticeDraft,
    NoticeFinding,
    NoticeStatus,
    ProviderResponseOutcome,
    RenderedVariant,
)

router = Router(tags=["app"], auth=customer_auth)

NOT_FOUND = "Notice not found"
ORIGINAL_NOT_FOUND = "Original not found"
FINDING_NOT_FOUND = "Finding not found"
#: `create_notice` raises these as `services.NoticeError`; the endpoint turns each into the 404 an
#: unknown id gets, and the outbox handler into a refusal code. Never make the two answers differ.
NOT_FOUND_CODES: dict[str, str] = {
    "original_not_found": ORIGINAL_NOT_FOUND,
    "finding_not_found": FINDING_NOT_FOUND,
}

VariantCode = Literal["dsa_art16", "dmca_512c", "dmca_512d", "provider_policy"]
LanguageCode = Literal["en", "de"]
StatusCode = Literal[
    "draft",
    "customer_approved",
    "marked_sent_by_customer",
    "provider_responded",
    "withdrawn",
    "corrected",
]
OutcomeCode = Literal["removed", "refused", "no_response"]
ConfirmationKindCode = Literal[
    "rights",
    "identity",
    "authorisation",
    "exceptions",
    "recipient",
    "accuracy",
    "forwarding_warning",
]
ChannelKindCode = Literal[
    "dsa_art16",
    "dmca_512c",
    "dmca_512d",
    "provider_copyright_policy",
    "provider_copyright_policy_dmca_style",
    "abuse_email",
    "web_form",
    "forwarding_only",
    "none_known",
]
V1SupportCode = Literal[
    "primary",
    "delisting_draft",
    "forwarding_only",
    "additional_contact_only",
    "needs_classification",
    "not_supported",
]

assert set(RenderedVariant.values) == set(VariantCode.__args__)  # type: ignore[attr-defined]
assert set(NoticeStatus.values) == set(StatusCode.__args__)  # type: ignore[attr-defined]
assert set(ProviderResponseOutcome.values) == set(OutcomeCode.__args__)  # type: ignore[attr-defined]
assert set(ConfirmationKind.values) == set(ConfirmationKindCode.__args__)  # type: ignore[attr-defined]


class NoticeChannelOut(Schema):
    kind: ChannelKindCode
    support: V1SupportCode
    email: str | None
    url: str | None
    source: Literal["directory", "rdap"]
    checked_at: datetime | None
    recheck_due: bool
    channel_id: int | None
    forwards_to_site_operator: bool | None
    publishes_to_transparency_db: bool | None


class NoticeProviderOut(Schema):
    hostname: str
    slot: Literal["service", "network", "registrar"]
    provider_name: str
    nexus: Literal["eu_facing", "us_nexus", "other", "unknown"]
    function: str
    channels: list[NoticeChannelOut]
    suggested_channel_kind: ChannelKindCode | None


class NoticeFindingCandidateOut(Schema):
    page_id: str = Field(description="Public id (page_…).")
    site: str
    url: str
    availability: str
    last_checked: datetime | None
    extent: Literal["full_copy", "excerpt", "not_determined"]
    requires_exception_review: bool
    blockers: list[str] = Field(description="Notice-gate blocker codes; empty means eligible.")
    providers: list[NoticeProviderOut]


class NoticeCandidatesOut(Schema):
    original_id: str
    title: str
    findings: list[NoticeFindingCandidateOut]


class ClaimantIn(Schema):
    name: str = Field(max_length=200)
    email: str = Field(max_length=254)
    address: str = Field(default="", max_length=1000)
    phone: str = Field(default="", max_length=64)


class RecipientIn(Schema):
    """The suggested recipient and channel the customer adopted.

    The channel's forwarding and transparency-publication practice is deliberately **not** a field
    here: it is a fact staff sourced in the provider directory, and it decides whether the
    forwarding warning has to be confirmed. `complaint_channel_id` names which channel of this
    host was adopted (`channels[].channel_id` of `candidates`), and the server reads the practice
    from it (`marceline.notices.services._channel_facts`).
    """

    hostname: str = Field(max_length=255)
    provider_name: str = Field(default="", max_length=255)
    channel_kind: ChannelKindCode
    email: str = Field(default="", max_length=254)
    url: str = Field(default="", max_length=2000)
    complaint_channel_id: int | None = Field(
        default=None, description="Which directory channel of this host was adopted, if any."
    )


class CreateNoticeIn(Schema):
    original_id: str
    page_ids: list[str]
    recipient: RecipientIn
    variant: VariantCode
    language: LanguageCode
    claimant: ClaimantIn
    legitimate_source_url: str = Field(default="", max_length=2000)
    publication_date: date | None = None
    exception_confirmed_page_ids: list[str] = Field(
        default_factory=list,
        description="Pages of page_ids for which the customer separately ticked the exception/"
        "fair-use review (required whenever a copy is not a current full copy).",
    )


class NoticeFindingOut(Schema):
    page_id: str
    url: str
    availability: str
    extent: str
    requires_exception_review: bool
    exception_confirmed: bool
    included: bool


class ConfirmationIn(Schema):
    kind: ConfirmationKindCode
    version: str = Field(max_length=32)
    language: LanguageCode
    confirmed: bool


class ConfirmationOut(Schema):
    kind: ConfirmationKindCode
    version: str
    language: LanguageCode
    text: str
    confirmed_at: datetime


class RequiredConfirmationOut(Schema):
    kind: ConfirmationKindCode
    version: str
    en: str
    de: str


class NoticeOut(Schema):
    id: str = Field(description="Public id (notice_…).")
    original_id: str
    status: StatusCode
    is_sandbox: bool
    variant: VariantCode
    language: LanguageCode
    recipient_hostname: str
    recipient_provider_name: str
    channel_kind: ChannelKindCode
    recipient_email: str = Field(description="Empty when the channel is a web form, not email.")
    recipient_url: str = Field(description="Empty when the channel is email, not a web form.")
    work_title: str
    created_at: datetime
    approved_at: datetime | None
    marked_sent_at: datetime | None
    responded_at: datetime | None
    outcome: OutcomeCode | None
    removal_observed_at: datetime | None = Field(
        description="When an availability check first found every page this notice reports gone, "
        "after it was sent. Null while any of them is still up, or not checked since. Never set "
        "for a sandbox notice."
    )
    withdrawn_at: datetime | None
    withdrawal_reason: str | None
    corrected_into: str | None
    findings: list[NoticeFindingOut]
    required_confirmations: list[RequiredConfirmationOut]
    confirmations: list[ConfirmationOut]
    preview_text: str | None = Field(description="Live render before approval; null once approved.")
    approved_text: str | None


def _channel_out(view: ChannelView) -> NoticeChannelOut:
    return NoticeChannelOut(
        kind=cast(ChannelKindCode, view.kind),
        support=view.support,
        email=view.email or None,
        url=view.url or None,
        source=view.source,  # type: ignore[arg-type]
        checked_at=view.checked_at,
        recheck_due=view.recheck_due,
        channel_id=view.channel_id,
        forwards_to_site_operator=view.forwards_to_site_operator,
        publishes_to_transparency_db=view.publishes_to_transparency_db,
    )


def _provider_out(role: ProviderRole) -> NoticeProviderOut:
    return NoticeProviderOut(
        hostname=role.hostname,
        slot=role.slot,
        provider_name=role.provider_name,
        nexus=role.nexus,
        function=role.function,
        channels=[_channel_out(view) for view in role.channels],
        suggested_channel_kind=cast(ChannelKindCode, role.suggested.kind)
        if role.suggested
        else None,
    )


def _finding_out(row: NoticeFinding) -> NoticeFindingOut:
    return NoticeFindingOut(
        page_id=row.page.public_id,
        url=row.url_snapshot,
        availability=row.availability_snapshot,
        extent=row.extent_snapshot,
        requires_exception_review=row.requires_exception_review,
        exception_confirmed=row.exception_confirmed,
        included=row.included,
    )


def _notice_out(draft: NoticeDraft) -> NoticeOut:
    required = services.required_confirmations(draft)
    confirmations = {row.kind: row for row in draft.confirmations.all()}
    live = draft.status == NoticeStatus.DRAFT
    return NoticeOut(
        id=draft.public_id,
        original_id=draft.original_public_id,
        status=cast(StatusCode, draft.status),
        is_sandbox=draft.is_sandbox,
        variant=cast(VariantCode, draft.rendered_variant),
        language=cast(LanguageCode, draft.language),
        recipient_hostname=draft.recipient_hostname,
        recipient_provider_name=draft.recipient_provider_name,
        channel_kind=cast(ChannelKindCode, draft.channel_kind),
        recipient_email=draft.recipient_email,
        recipient_url=draft.recipient_url,
        work_title=draft.work_title,
        created_at=draft.created_at,
        approved_at=draft.approved_at,
        marked_sent_at=draft.marked_sent_at,
        responded_at=draft.responded_at,
        outcome=cast(OutcomeCode, draft.outcome) or None,
        removal_observed_at=draft.hoster_contact.removed_observed_at
        if draft.hoster_contact
        else None,
        withdrawn_at=draft.withdrawn_at,
        withdrawal_reason=draft.withdrawal_reason or None,
        corrected_into=draft.corrected_into.public_id if draft.corrected_into else None,
        findings=[_finding_out(row) for row in draft.findings.all().select_related("page")],
        required_confirmations=[
            RequiredConfirmationOut(
                kind=cast(ConfirmationKindCode, kind),
                version=declarations.current(kind).version,
                en=declarations.current(kind).text("en"),
                de=declarations.current(kind).text("de"),
            )
            for kind in required
        ],
        confirmations=[
            ConfirmationOut(
                kind=cast(ConfirmationKindCode, row.kind),
                version=row.version,
                language=cast(LanguageCode, row.language),
                text=row.text,
                confirmed_at=row.confirmed_at,
            )
            for row in confirmations.values()
        ],
        preview_text=services.render_preview(draft) if live else None,
        approved_text=draft.approved_text or None,
    )


def _actor(request: HttpRequest) -> services.Actor:
    return services.Actor.customer(cast(User, request.user))


def _drafts_of(organisation: Organisation) -> QuerySet[NoticeDraft]:
    return (
        NoticeDraft.objects.filter(organisation=organisation)
        .prefetch_related("findings__page", "confirmations")
        .select_related("corrected_into", "hoster_contact")
    )


def draft_for(organisation: Organisation | None, notice_id: str) -> NoticeDraft | None:
    """One draft of this organisation, if it may still be shown at all.

    A draft that reports a page the customer may no longer see (`services.visible`: newly
    safety-flagged, or below the media gate) answers `None` here, so the detail view and every
    action on it give the same 404 as an unknown id — never a hint that something is hidden.

    The one lookup: the endpoints on development, the bundle `marceline/export.py` renders for
    production, and every outbox handler development applies (`marceline/customer_writes.py`) go
    through it, so a write accepted on production is refused again there if the draft has
    meanwhile become one this organisation may not see.
    """
    if organisation is None or not public_ids.is_valid(NOTICE_ID_PREFIX, notice_id):
        return None
    draft = _drafts_of(organisation).filter(public_id=notice_id).first()
    if draft is None or not services.visible(draft):
        return None
    return draft


def visible_draft_ids(organisation: Organisation) -> list[str]:
    """The public ids of the drafts this organisation's list shows, newest first.

    What `marceline/export.py` builds the bundle's notice detail and download documents from, so
    the documents production can serve are exactly the drafts the list names.
    """
    drafts = _drafts_of(organisation).order_by("-created_at", "-pk")
    return [draft.public_id for draft in drafts if services.visible(draft)]


# --- writes: applied here on development, accepted into the outbox on production (ADR 0046) -----


class NoticeWriteAcceptedOut(Schema):
    """Production's answer to a notice write it cannot apply itself (ADR 0028 write path,
    ADR 0046): accepted into the outbox, development applies it shortly.

    Production holds no findings, matches, rights records or provider directory, so it cannot
    judge a notice write — it can only take it down. The customer sees the pending state and
    follows the outcome in `GET /app/notices/submissions`; nothing is claimed to have happened.
    """

    pending: bool = Field(default=True, description="Accepted; not yet applied.")
    submission_id: str = Field(description="Follow it in GET /app/notices/submissions (sub_…).")


SubmissionStatus = Literal["processing", "done", "refused", "failed"]
SUBMISSION_STATUS: dict[str, SubmissionStatus] = {
    OutboxItem.Status.PENDING: "processing",
    OutboxItem.Status.DONE: "done",
    OutboxItem.Status.REFUSED: "refused",
    OutboxItem.Status.QUARANTINED: "failed",
}
#: The notice writes a customer can start; `notice_exported` is an audit record of a download and
#: is deliberately not listed (nothing is waiting for the customer there).
SUBMITTED_KINDS: tuple[str, ...] = (
    OutboxItem.Kind.NOTICE_CREATE,
    OutboxItem.Kind.NOTICE_APPROVE,
    OutboxItem.Kind.NOTICE_MARK_SENT,
    OutboxItem.Kind.NOTICE_RESPONSE,
    OutboxItem.Kind.NOTICE_WITHDRAW,
    OutboxItem.Kind.NOTICE_CORRECT,
)
MAX_SUBMISSIONS = 50


class NoticeSubmissionOut(Schema):
    id: str = Field(description="Opaque id of this submission (`sub_…`).")
    kind: str = Field(description="Which notice write this was (`notice_approve`, …).")
    status: SubmissionStatus = Field(
        description="`processing` until it has been applied or refused; `failed` means it could "
        "not be applied automatically and a person will look at it."
    )
    code: str = Field(description="The outcome's code, empty while it is processing.")
    notice_id: str | None = Field(description="The draft this write is about, once it is known.")
    received_at: datetime


def writes_available() -> bool:
    """This machine takes notice writes: development directly, production into the outbox."""
    return settings.MARCELINE_ROLE != "prod" or outbox.writes_enabled()


def into_outbox() -> bool:
    """Writes here are accepted into the outbox rather than applied (production)."""
    return settings.MARCELINE_ROLE == "prod"


def _gate_or_enqueue(
    request: HttpRequest,
    organisation: Organisation,
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str = "",
) -> Status[NoticeWriteAcceptedOut] | Status[AppErrorOut] | None:
    """On production, take the write into the outbox instead of applying it: 202, or 503/429
    before anything is queued. `None` on development, where the caller goes on as before.

    Nothing here decides whether the write is *allowed* — the notice gate, the safety filter and
    the state machine all need data only development has, and they run again there. A write
    accepted here can still be refused; the customer reads that in `submissions`.
    """
    if not into_outbox():
        return None
    if not writes_available():
        return Status(503, AppErrorOut(detail="notice_write_unavailable"))
    now = timezone.now()
    if outbox.paused(now):
        return Status(503, AppErrorOut(detail="processing_paused"))
    user = cast(User, request.user)
    if upload_throttle.admit(organisation.pk, upload_throttle.Kind.NOTICE_WRITE, now=now):
        return Status(429, AppErrorOut(detail="too_many_requests"))
    item, _ = outbox.enqueue(
        kind=kind,
        organisation_id=organisation.pk,
        user_id=user.pk,
        payload=payload,
        now=now,
        dedupe_key=dedupe_key,
    )
    return Status(202, NoticeWriteAcceptedOut(submission_id=item.public_id))


@router.get("/candidates/{original_id}", response={200: NoticeCandidatesOut, 404: AppErrorOut})
def candidates(request: HttpRequest, original_id: str) -> Status[NoticeCandidatesOut | AppErrorOut]:
    """Every confirmed finding of this record, with its notice-gate blockers and suggested
    provider/channel — what the "Prepare notice draft" screen builds its per-URL list from."""
    organisation = organisation_of(request.user)
    original = owned_original(organisation, original_id)
    if organisation is None or original is None:
        return Status(404, AppErrorOut(detail=ORIGINAL_NOT_FOUND))
    reference = original.representative
    rows = confirmed_findings_of([original]).get(original.pk, [])
    now = timezone.now()
    # Every reference of the file this organisation owns, not just the representative: the
    # confirmation may sit on a twin, and a rejection on any of them vetoes (`gate.file_matches`).
    matches = file_matches(organisation, reference.sha256, [row.page.pk for row in rows])
    out = []
    for row in rows:
        match = matches.get(row.page.pk)
        blockers = finding_gate_blockers(organisation, reference, row.page, match, now=now)
        hostname = normalize_hostname(row.page.site.domain)
        roles = provider_roles([hostname], now).get(hostname, [])
        out.append(
            NoticeFindingCandidateOut(
                page_id=row.page.public_id,
                site=row.page.site.domain,
                url=row.page.url,
                availability=row.page.availability,
                last_checked=row.page.availability_checked_at,
                extent=extent_of(row),
                requires_exception_review=match.requires_exception_review if match else True,
                blockers=blockers,
                providers=[_provider_out(role) for role in roles],
            )
        )
    return Status(
        200,
        NoticeCandidatesOut(original_id=original.public_id, title=reference.title, findings=out),
    )


@router.get("", response=list[NoticeOut])
def notices(request: HttpRequest) -> list[NoticeOut]:
    """Every notice draft of this organisation, newest first."""
    organisation = organisation_of(request.user)
    if organisation is None:
        return []
    drafts = _drafts_of(organisation).order_by("-created_at", "-pk")
    return [_notice_out(draft) for draft in drafts if services.visible(draft)]


def create_notice(
    organisation: Organisation, payload: CreateNoticeIn, actor: services.Actor
) -> NoticeDraft:
    """Prepare one draft from a customer's request — the one implementation.

    This endpoint runs it on development, and `marceline/customer_writes.py` runs it for every
    draft production accepted into its outbox. Nothing is decided here that production could have
    decided: the original and the pages are looked up in the data only development holds, and
    `services.create_draft` re-checks the notice gate for every selected finding.

    Raises `services.NoticeError`; the two codes in `NOT_FOUND_CODES` are the ones the endpoint
    turns back into the 404 an unknown id gets.
    """
    original = owned_original(organisation, payload.original_id)
    if original is None:
        raise services.NoticeError("original_not_found")
    pages_by_public_id = {
        row.page.public_id: row.page.pk
        for row in confirmed_findings_of([original]).get(original.pk, [])
    }
    page_ids = []
    for page_public_id in payload.page_ids:
        pk = pages_by_public_id.get(page_public_id)
        if pk is None:
            raise services.NoticeError("finding_not_found")
        page_ids.append(pk)
    exception_confirmed_pages = set()
    for page_public_id in payload.exception_confirmed_page_ids:
        pk = pages_by_public_id.get(page_public_id)
        if pk is None:
            raise services.NoticeError("finding_not_found")
        exception_confirmed_pages.add(pk)
    recipient = services.RecipientChoice(
        hostname=payload.recipient.hostname,
        provider_name=payload.recipient.provider_name,
        channel_kind=payload.recipient.channel_kind,
        email=payload.recipient.email,
        url=payload.recipient.url,
        complaint_channel_id=payload.recipient.complaint_channel_id,
    )
    claimant = services.ClaimantInput(
        name=payload.claimant.name,
        email=payload.claimant.email,
        address=payload.claimant.address,
        phone=payload.claimant.phone,
    )
    return services.create_draft(
        organisation,
        original.representative,
        page_ids,
        recipient,
        payload.variant,
        payload.language,
        claimant,
        work_title=original.representative.title,
        original_public_id=original.public_id,
        legitimate_source_url=payload.legitimate_source_url,
        publication_date=payload.publication_date,
        exception_confirmed_pages=frozenset(exception_confirmed_pages),
        actor=actor,
    )


@router.post(
    "",
    response={
        200: NoticeOut,
        202: NoticeWriteAcceptedOut,
        404: AppErrorOut,
        409: AppErrorOut,
        429: AppErrorOut,
        503: AppErrorOut,
    },
)
def create(
    request: HttpRequest, payload: CreateNoticeIn
) -> Status[NoticeOut] | Status[NoticeWriteAcceptedOut] | Status[AppErrorOut]:
    """Prepare a draft for one recipient and channel, grouping the selected findings.

    409 `finding_not_eligible` (with the blockers per page id) when any selected URL is not
    currently eligible; the customer drops it from the selection and tries again. 409
    `unknown_channel` when `complaint_channel_id` is not a channel the directory offers for this
    host any more, and 409 `unknown_recipient` when `recipient.hostname` is not a host of the
    selected findings — the screen refetches the candidates and picks again.

    202 on production: accepted into the outbox — the findings, the matches and the provider
    directory live on development, so whether this draft may be prepared at all is decided when
    development applies it, and the outcome appears in `GET /app/notices/submissions`.
    """
    organisation = organisation_of(request.user)
    if organisation is None:
        return Status(404, AppErrorOut(detail=ORIGINAL_NOT_FOUND))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.NOTICE_CREATE,
        payload.model_dump(mode="json"),
        # A double submit of the same draft while the first is still pending is one write.
        dedupe_key=(
            f"{payload.original_id}|{payload.recipient.hostname}|{payload.recipient.channel_kind}"
        ),
    )
    if gate is not None:
        return gate
    try:
        draft = create_notice(organisation, payload, _actor(request))
    except services.NoticeError as error:
        if error.code in NOT_FOUND_CODES:
            return Status(404, AppErrorOut(detail=NOT_FOUND_CODES[error.code]))
        return Status(409, AppErrorOut(detail=error.code))
    return Status(200, _notice_out(draft))


# Registered before `/{notice_id}`: Django matches URL patterns in order, and the detail
# route would otherwise swallow `submissions` and answer its 404.
@router.get("/submissions", response=list[NoticeSubmissionOut])
def submissions(request: HttpRequest) -> list[NoticeSubmissionOut]:
    """Notice writes this organisation sent that are being applied, or were in the last days.

    Production accepts a notice write and development applies it a minute later (ADR 0046); this
    list is how the app shows that, and what became of it — including a refusal only development
    can decide, because only it holds the findings, the gate and the draft's state. Empty on
    development, where a write is applied at once, and for an account without an organisation.
    """
    membership = membership_of(request.user)
    if membership is None:
        return []
    items = OutboxItem.objects.filter(
        organisation_id=membership.organisation_id, kind__in=SUBMITTED_KINDS
    ).order_by("-received_at", "-pk")[:MAX_SUBMISSIONS]
    return [
        NoticeSubmissionOut(
            id=item.public_id,
            kind=item.kind,
            status=SUBMISSION_STATUS[item.status],
            code=item.result_code,
            notice_id=item.original_id or None,
            received_at=item.received_at,
        )
        for item in items
    ]


@router.get("/{notice_id}", response={200: NoticeOut, 404: AppErrorOut})
def notice_detail(request: HttpRequest, notice_id: str) -> Status[NoticeOut | AppErrorOut]:
    draft = draft_for(organisation_of(request.user), notice_id)
    if draft is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    return Status(200, _notice_out(draft))


EXPORT_OPENAPI = {
    "responses": {
        "200": {
            "description": "The approved notice as a file (attachment): the text itself, an "
            "unsent .eml message, or the evidence annex.",
            "content": {
                "text/plain": {"schema": {"type": "string"}},
                "message/rfc822": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    }
}


@router.get(
    "/{notice_id}/export/{fmt}",
    response={404: AppErrorOut, 409: AppErrorOut},
    openapi_extra=EXPORT_OPENAPI,
)
def export_file(
    request: HttpRequest, notice_id: str, fmt: str
) -> HttpResponse | Status[AppErrorOut]:
    """Download the approved notice: `txt` (the exact approved text), `eml` (an unsent message to
    open in the customer's own mail program) or `annex` (the evidence annex).

    Nothing is transmitted here — the file is bytes for the browser, and `sandbox_send` stays the
    only path that hands a draft to a mail transport. An unknown format is the same 404 as an
    unknown id. 409 `not_approved` before the confirmations are made (an export may never be a
    way past them) and `already_closed` for a withdrawn or corrected draft.

    On production the bytes come from the signed bundle (`_export_from_cache`): a download is the
    one customer read whose record is a write, so the view mediates it instead of the cache
    middleware — it takes the document and appends one `notice_exported` outbox item, which is how
    the `exported` event reaches the append-only log on development.
    """
    # Rendering a bundle is development answering out of its own database, whatever the role of
    # the process doing it — it is what *fills* the cache, so it must never read from it.
    rendering_bundle = bool(getattr(request, "bundle_render", False))
    if serving_from_cache() and not rendering_bundle:
        return _export_from_cache(request, notice_id, fmt)
    draft = draft_for(organisation_of(request.user), notice_id)
    if draft is None or fmt not in export.FORMATS:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    try:
        # A bundle render is not a download the customer made, so it records nothing.
        if rendering_bundle:
            file = services.export_payload(draft, fmt)
        else:
            file = services.export_notice(draft, fmt, _actor(request))
    except services.NoticeError as error:
        return Status(409, AppErrorOut(detail=error.code))
    return download_response(
        notice_id, fmt, status=200, content_type=file.content_type, content=file.content
    )


def serving_from_cache() -> bool:
    """Customer data on this machine comes from the signed bundle, not from local tables."""
    return settings.MARCELINE_ROLE == "prod"


def download_response(
    notice_id: str, fmt: str, *, status: int, content_type: str, content: bytes
) -> HttpResponse:
    """One answer shape for a download, whichever machine produced the bytes.

    The file name is `export.filename_for`, so what a customer's browser saves never depends on
    where the bytes came from; a refusal (404/409, JSON) carries no disposition.
    """
    response = HttpResponse(content, status=status, content_type=content_type)
    if status == 200:
        filename = export.filename_for(notice_id, fmt)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


def _export_from_cache(
    request: HttpRequest, notice_id: str, fmt: str
) -> HttpResponse | Status[AppErrorOut]:
    """Production: the download, byte for byte as development rendered it (ADR 0046).

    The bundle carries one document per visible draft and format — the 200 with the bytes, or the
    very 409 development would answer for a draft that is not approved or already closed — and
    `notices/*` is the 404 an unknown id, a foreign id or an unknown format gets, the same
    byte-identical answer as an unknown draft.
    """
    membership = membership_of(request.user)
    if membership is None:
        # An account with no organisation owns no draft: the same 404 as an unknown id, rendered
        # by the same schema development renders it with.
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    known = public_ids.is_valid(NOTICE_ID_PREFIX, notice_id) and fmt in export.FORMATS
    key = f"notices/{notice_id}/export/{fmt}" if known else "notices/*"
    try:
        document = store.document_for(
            membership.organisation_id,
            key,
            fallback_key="notices/*" if known else None,
            now=timezone.now(),
        )
    except store.Unavailable:
        return customer_cache.unavailable_response()
    if document.status == 200:
        # The customer really downloaded it: development appends the `exported` event to the
        # append-only log, which only it holds. Never deduplicated — each download is its own row.
        outbox.enqueue(
            kind=OutboxItem.Kind.NOTICE_EXPORTED,
            organisation_id=membership.organisation_id,
            user_id=cast(User, request.user).pk,
            payload={"notice_id": notice_id, "format": fmt},
            now=timezone.now(),
        )
    response = download_response(
        notice_id,
        fmt,
        status=document.status,
        content_type=document.content_type,
        content=document.body.encode(),
    )
    response["X-Data-As-Of"] = customer_cache.as_of_header(document.as_of)
    return response


WRITE_RESPONSES: dict[int, Any] = {
    200: NoticeOut,
    202: NoticeWriteAcceptedOut,
    404: AppErrorOut,
    409: AppErrorOut,
    429: AppErrorOut,
    503: AppErrorOut,
}


@router.post("/{notice_id}/approve", response=WRITE_RESPONSES)
def approve(
    request: HttpRequest, notice_id: str, payload: list[ConfirmationIn]
) -> Status[NoticeOut] | Status[NoticeWriteAcceptedOut] | Status[AppErrorOut]:
    """Confirm every required declaration and freeze the exact rendered text.

    409 `confirmation_incomplete` (with the missing kinds) or `finding_no_longer_eligible` (with
    the blockers per page id) when the world changed since the draft was created.

    202 on production: accepted into the outbox. The eligibility re-check and the text freeze need
    the findings and the gate, which live on development, so it happens when development applies
    the write — a draft that stopped being eligible in the meantime is refused there, and the
    refusal appears in `GET /app/notices/submissions`.
    """
    organisation = organisation_of(request.user)
    if organisation is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.NOTICE_APPROVE,
        {
            "notice_id": notice_id,
            "confirmations": [item.model_dump(mode="json") for item in payload],
        },
        dedupe_key=notice_id,
    )
    if gate is not None:
        return gate
    draft = draft_for(organisation, notice_id)
    if draft is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    inputs = [
        services.ConfirmationInput(
            kind=item.kind, version=item.version, language=item.language, confirmed=item.confirmed
        )
        for item in payload
    ]
    try:
        draft = services.approve(draft, inputs, _actor(request))
    except services.NoticeError as error:
        return Status(409, AppErrorOut(detail=error.code))
    return Status(200, _notice_out(draft))


@router.post("/{notice_id}/mark-sent", response=WRITE_RESPONSES)
def mark_sent(
    request: HttpRequest, notice_id: str
) -> Status[NoticeOut] | Status[NoticeWriteAcceptedOut] | Status[AppErrorOut]:
    """ "I sent this myself." 409 `sandbox_cannot_mark_real_send` for a sandbox organisation.

    202 on production: accepted into the outbox, applied by development shortly. Marceline still
    sends nothing here or anywhere else — this only records what the customer did.
    """
    organisation = organisation_of(request.user)
    if organisation is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.NOTICE_MARK_SENT,
        {"notice_id": notice_id},
        dedupe_key=notice_id,
    )
    if gate is not None:
        return gate
    draft = draft_for(organisation, notice_id)
    if draft is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    try:
        draft = services.mark_sent(draft, _actor(request))
    except services.NoticeError as error:
        return Status(409, AppErrorOut(detail=error.code))
    return Status(200, _notice_out(draft))


class SandboxSendIn(Schema):
    recipient_email: str = Field(max_length=254)


# `sandbox-send` comes **before** the id in the path on purpose: `ROLE_ONLY_PREFIXES` is a prefix
# allowlist, and this is the one path in the customer API that may hand a draft to a mail
# transport, so the public host must refuse it before anything routes (ADR 0046).
@router.post(
    "/sandbox-send/{notice_id}",
    response={200: NoticeOut, 404: AppErrorOut, 409: AppErrorOut},
)
def sandbox_send(
    request: HttpRequest, notice_id: str, payload: SandboxSendIn
) -> Status[NoticeOut | AppErrorOut]:
    """Sandbox organisations only: an email copy to a `NOTICE_SANDBOX_RECIPIENTS` address.

    409 `not_sandbox`, `recipient_not_allowlisted`, or `not_approved`.
    """
    draft = draft_for(organisation_of(request.user), notice_id)
    if draft is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    try:
        draft = services.sandbox_send(draft, _actor(request), payload.recipient_email)
    except services.NoticeError as error:
        return Status(409, AppErrorOut(detail=error.code))
    return Status(200, _notice_out(draft))


class ResponseIn(Schema):
    outcome: OutcomeCode
    responded_at: datetime | None = None


@router.post("/{notice_id}/response", response=WRITE_RESPONSES)
def record_response(
    request: HttpRequest, notice_id: str, payload: ResponseIn
) -> Status[NoticeOut] | Status[NoticeWriteAcceptedOut] | Status[AppErrorOut]:
    """Record what the provider answered. 202 on production: accepted into the outbox."""
    organisation = organisation_of(request.user)
    if organisation is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.NOTICE_RESPONSE,
        {"notice_id": notice_id, **payload.model_dump(mode="json")},
        dedupe_key=notice_id,
    )
    if gate is not None:
        return gate
    draft = draft_for(organisation, notice_id)
    if draft is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    try:
        draft = services.record_provider_response(
            draft,
            payload.outcome,
            _actor(request),
            responded_at=payload.responded_at,
        )
    except services.NoticeError as error:
        return Status(409, AppErrorOut(detail=error.code))
    return Status(200, _notice_out(draft))


class WithdrawIn(Schema):
    reason: str = Field(max_length=32)
    note: str = Field(default="", max_length=500)


@router.post("/{notice_id}/withdraw", response=WRITE_RESPONSES)
def withdraw(
    request: HttpRequest, notice_id: str, payload: WithdrawIn
) -> Status[NoticeOut] | Status[NoticeWriteAcceptedOut] | Status[AppErrorOut]:
    """Take a notice back. 202 on production: accepted into the outbox."""
    organisation = organisation_of(request.user)
    if organisation is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.NOTICE_WITHDRAW,
        {"notice_id": notice_id, **payload.model_dump(mode="json")},
        dedupe_key=notice_id,
    )
    if gate is not None:
        return gate
    draft = draft_for(organisation, notice_id)
    if draft is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    try:
        draft = services.withdraw(draft, payload.reason, payload.note, _actor(request))
    except services.NoticeError as error:
        return Status(409, AppErrorOut(detail=error.code))
    return Status(200, _notice_out(draft))


class CorrectIn(Schema):
    reason: str = Field(max_length=32)
    note: str = Field(default="", max_length=2000)


@router.post("/{notice_id}/correct", response=WRITE_RESPONSES)
def correct(
    request: HttpRequest, notice_id: str, payload: CorrectIn
) -> Status[NoticeOut] | Status[NoticeWriteAcceptedOut] | Status[AppErrorOut]:
    """Close this draft as `corrected` and open a fresh one with the same findings, to redo.

    202 on production: accepted into the outbox; the successor appears in the list once
    development has applied it.
    """
    organisation = organisation_of(request.user)
    if organisation is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    gate = _gate_or_enqueue(
        request,
        organisation,
        OutboxItem.Kind.NOTICE_CORRECT,
        {"notice_id": notice_id, **payload.model_dump(mode="json")},
        dedupe_key=notice_id,
    )
    if gate is not None:
        return gate
    draft = draft_for(organisation, notice_id)
    if draft is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    try:
        successor = services.correct(draft, payload.reason, payload.note, _actor(request))
    except services.NoticeError as error:
        return Status(409, AppErrorOut(detail=error.code))
    return Status(200, _notice_out(successor))
