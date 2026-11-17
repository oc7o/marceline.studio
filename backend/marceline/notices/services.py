"""The only writer of the notice tables (issue #33, ADR 0035).

Status machine: `draft -> customer_approved -> marked_sent_by_customer -> provider_responded`, with
`withdrawn` / `corrected` reachable from any open status. A sandbox draft (organisation.is_sandbox,
snapshotted at creation) can only reach `marked_sent_by_customer` through `sandbox_send` — the
ordinary `mark_sent` (the ordinary customer's "I sent this myself") is refused for it, and its
provider responses are never fed into `catalog.HosterContact`, so a sandbox test notice never
touches real provider statistics.

Every write logs a `NoticeEvent`; `approve` additionally freezes the exact rendered text and its
sha256 on the draft and in the event (answer 01: "store the exact text/hash of the draft the
customer approved").

Two facts are **never taken from the caller**: whether each reported page is still one the
customer may see at all (`visible`, ADR 0011/0018 — a draft is the one record that keeps a URL
snapshot of its own), and whether the chosen channel forwards a complaint to the site operator or
publishes it (`_channel_facts`, read from the provider directory, because it decides which
declarations must be confirmed).
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from accounts.models import Organisation
from catalog.hosting.directory import provider_roles
from catalog.hosting.services import normalize_hostname
from catalog.models import HosterContact, HostingRecord
from core.public_ids import missing
from index.models import Page
from marceline.app.ownership import owned_references, representative_rank
from marceline.app.safety import page_visible
from marceline.notices import declarations, export
from marceline.notices.gate import file_matches, finding_gate_blockers
from marceline.notices.models import (
    CLOSED_NOTICE_STATUSES,
    REQUIRED_CONFIRMATIONS,
    ConfirmationKind,
    NoticeConfirmation,
    NoticeDraft,
    NoticeEvent,
    NoticeEventKind,
    NoticeFinding,
    NoticeStatus,
    ProviderResponseOutcome,
    RenderedVariant,
)
from marceline.notices.renderers import (
    NoticeContext,
    NoticeFindingFact,
    render,
    split_subject,
)
from marceline.rights import services as rights_services
from marceline.rights.services import SYSTEM, Actor
from matching.copy_extent import CopyExtent
from matching.fingerprint import KIND_VIDEO
from matching.models import Reference

__all__ = ["SYSTEM", "Actor"]


class NoticeError(Exception):
    """A refused notice action, with a machine-readable code.

    `detail` carries the extra machine-readable payload some codes need (blockers per page id
    for `finding_not_eligible` / `finding_no_longer_eligible`, missing kinds for
    `confirmation_incomplete`) — never prose, and never anything a customer response echoes
    unfiltered.
    """

    def __init__(self, code: str, detail: Any = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


OPEN_STATUSES = frozenset(
    {
        NoticeStatus.DRAFT,
        NoticeStatus.CUSTOMER_APPROVED,
        NoticeStatus.MARKED_SENT_BY_CUSTOMER,
        NoticeStatus.PROVIDER_RESPONDED,
    }
)


@dataclass(frozen=True)
class ClaimantInput:
    name: str
    email: str
    address: str = ""
    phone: str = ""


@dataclass(frozen=True)
class RecipientChoice:
    """The recipient and channel the customer adopted from the suggestions.

    Deliberately without the forwarding and transparency-publication flags: those are facts staff
    sourced in the provider directory, not something a request states (`_channel_facts`).
    """

    hostname: str
    provider_name: str
    channel_kind: str
    email: str = ""
    url: str = ""
    complaint_channel_id: int | None = None


def _log(
    draft: NoticeDraft, kind: str, actor: Actor, *, detail: dict[str, object] | None = None
) -> None:
    NoticeEvent.objects.create(
        draft=draft,
        kind=kind,
        actor_kind=actor.kind,
        actor_id=actor.user_id,
        actor_label=actor.label,
        detail=detail or {},
    )


def visible(draft: NoticeDraft) -> bool:
    """Whether this draft may still be shown to, or acted on by, its customer.

    A draft keeps each reported URL in a snapshot of its own, so it follows the rule every other
    customer response follows: one page that stopped being visible — newly safety-flagged, or
    dropped below the media gate — hides the whole draft, exactly as a case disappears with its
    last visible finding (ADR 0011, ADR 0018, ADR 0025). The record is never deleted: it is the
    audit trail of what the customer confirmed, and only staff read it from then on.
    """
    return all(page_visible(row.page) for row in draft.findings.all())


def _require_visible(draft: NoticeDraft) -> None:
    """The writer's own chokepoint for `visible`; the same answer as an unknown page."""
    if not visible(draft):
        raise NoticeError("unknown_page")


def _reference_of(draft: NoticeDraft) -> Reference | None:
    """This organisation's current original for the draft's file, or None.

    The very reference `create_draft` was handed: the endpoint passes the original's
    representative (`ownership.owned_original`), so `approve` has to re-check that one and no
    other. A re-fingerprinted file has several references (ADR 0022), and asking for "any
    reference with this sha256" left the re-check to row order — a match a human **rejected** on
    the representative could be stood in for by a confirmed one on a superseded twin, which is
    the notice gate's one human step. Scoping by the owner also refuses a draft for a file the
    organisation no longer owns.
    """
    references = list(owned_references(draft.organisation).filter(sha256=draft.sha256))
    return max(references, key=representative_rank) if references else None


def required_confirmations(draft: NoticeDraft) -> tuple[str, ...]:
    """Which confirmations this draft needs before it may be approved, in a fixed order.

    The forwarding/publication warning is required only when the recipient is flagged (or
    unknown) for it — not explicitly cleared `False` (answer 01, Part Three "What the customer
    should see before sending").
    """
    warning_needed = (
        draft.forwards_to_site_operator is not False
        or draft.publishes_to_transparency_db is not False
    )
    return tuple(
        kind
        for kind in REQUIRED_CONFIRMATIONS
        if kind != ConfirmationKind.FORWARDING_WARNING or warning_needed
    )


def _channel_facts(recipient: RecipientChoice, now: datetime) -> tuple[bool | None, bool | None]:
    """(forwards to the site operator, publishes to a transparency database) for this channel.

    Read from the provider directory, **never from the request**. Both are practice the operator
    recorded with a source URL the database insists on (`catalog.ComplaintChannel`), and together
    they decide whether the customer has to confirm the forwarding warning before approving
    (`required_confirmations`): a caller who could state them would be waiving their own warning
    that the notice — with their name and address in it — reaches the site's operator or a public
    database. A channel the directory does not offer for this host (unknown, superseded, or
    another provider's) is refused rather than trusted, which also keeps an arbitrary integer out
    of the foreign key. Without a named channel both stay unknown, which requires the warning.
    """
    if recipient.complaint_channel_id is None:
        return None, None
    hostname = normalize_hostname(recipient.hostname)
    for role in provider_roles([hostname], now).get(hostname, []):
        for view in role.channels:
            if view.channel_id == recipient.complaint_channel_id:
                return view.forwards_to_site_operator, view.publishes_to_transparency_db
    raise NoticeError("unknown_channel")


@transaction.atomic
def create_draft(
    organisation: Organisation,
    reference: Reference,
    page_ids: Sequence[int],
    recipient: RecipientChoice,
    variant: str,
    language: str,
    claimant: ClaimantInput,
    *,
    work_title: str,
    original_public_id: str,
    legitimate_source_url: str = "",
    publication_date: date | None = None,
    exception_confirmed_pages: frozenset[int] = frozenset(),
    actor: Actor,
    now: datetime | None = None,
) -> NoticeDraft:
    """Create a canonical draft for one recipient and channel, grouping the given findings.

    Every finding is re-checked against the notice gate at creation time; any blocker at all
    refuses the whole draft with `finding_not_eligible` (never a partial draft of only the clean
    URLs — the customer chooses which findings to include before creating it).

    `exception_confirmed_pages` names the pages for which the customer has separately ticked the
    per-finding exception/fair-use review (answer 01: required in addition to the batch
    confirmation whenever a copy is not a current full copy). A page needing that tick without it
    is not eligible and is refused with the rest.

    Three things the caller does not get to decide: a page it may not see at all is `unknown_page`
    (`page_visible`), the chosen channel's forwarding and publication practice comes from the
    directory (`_channel_facts`, `unknown_channel`), and the recipient must be a provider of a
    host this draft reports (`unknown_recipient`).
    """
    now = now or timezone.now()
    if variant not in RenderedVariant.values:
        raise NoticeError("unknown_variant")
    if language not in ("en", "de"):
        raise NoticeError("unknown_language")
    if not page_ids:
        raise NoticeError("no_findings_selected")
    pages = list(Page.objects.filter(pk__in=page_ids).select_related("site"))
    # A page a customer may not see at all (safety-flagged, or below the media gate) is not a
    # finding and must never enter a draft, whose findings keep a URL snapshot of their own
    # (ADR 0011, ADR 0018). Same answer as an id that does not exist: never say which applied.
    if len(pages) != len(set(page_ids)) or not all(page_visible(page) for page in pages):
        raise NoticeError("unknown_page")
    forwards, publishes = _channel_facts(recipient, now)
    matches = file_matches(organisation, reference.sha256, page_ids)
    blockers_by_page = {}
    for page in pages:
        blockers = finding_gate_blockers(
            organisation,
            reference,
            page,
            matches.get(page.pk),
            exception_confirmed=page.pk in exception_confirmed_pages,
            now=now,
        )
        if blockers:
            blockers_by_page[page.public_id] = blockers
    if blockers_by_page:
        raise NoticeError("finding_not_eligible", blockers_by_page)
    # The recipient must be a provider of a host this draft actually reports — the hosts
    # `candidates` offers roles for. Otherwise a caller could address a draft to any hostname at
    # all, and a provider response recorded on it would write `catalog.HosterContact`, the
    # response history *other* organisations are shown (ADR 0027). Widen this together with what
    # `marceline/app/notices.py candidates` offers.
    if normalize_hostname(recipient.hostname) not in {
        normalize_hostname(page.site.domain) for page in pages
    }:
        raise NoticeError("unknown_recipient")

    record = rights_services.current_work(organisation, reference.sha256)
    draft = NoticeDraft.objects.create(
        organisation=organisation,
        is_sandbox=organisation.is_sandbox,
        language=language,
        rendered_variant=variant,
        recipient_hostname=recipient.hostname,
        recipient_provider_name=recipient.provider_name,
        channel_kind=recipient.channel_kind,
        recipient_email=recipient.email,
        recipient_url=recipient.url,
        complaint_channel_id=recipient.complaint_channel_id,
        forwards_to_site_operator=forwards,
        publishes_to_transparency_db=publishes,
        sha256=reference.sha256,
        original_public_id=original_public_id,
        work_title=work_title,
        legitimate_source_url=legitimate_source_url,
        publication_date=publication_date,
        claimant_name=claimant.name,
        claimant_capacity=record.capacity if record else "",
        underlying_owner_name=record.underlying_owner_name if record else "",
        claimant_email=claimant.email,
        claimant_address=claimant.address,
        claimant_phone=claimant.phone,
        asserted_right=record.asserted_right if record else "",
        asserted_right_detail=record.asserted_right_detail if record else "",
        created_at=now,
    )
    for page in pages:
        match = matches.get(page.pk)
        NoticeFinding.objects.create(
            draft=draft,
            page=page,
            url_snapshot=page.url,
            first_seen_at=page.first_seen,
            last_checked_at=page.availability_checked_at,
            # The human review the gate insisted on, dated (issue #142). The gate has already
            # refused a finding with no confirmed high row, so this is None only for a decision
            # taken before `ReferenceMatch.decided_at` existed and not backfilled — in which case
            # the paragraph says nothing rather than inventing a date.
            reviewed_at=match.confirmed_at if match else None,
            availability_snapshot=page.availability,
            # The file's most cautious extent (`gate.FileMatch`), so the snapshot matches what
            # the customer was shown rather than whichever reference's row came back first.
            extent_snapshot=str(match.extent if match else CopyExtent.UNKNOWN),
            requires_exception_review=match.requires_exception_review if match else True,
            exception_confirmed=page.pk in exception_confirmed_pages,
        )
    _log(
        draft, NoticeEventKind.CREATED, actor, detail={"pages": [page.public_id for page in pages]}
    )
    return draft


@dataclass(frozen=True)
class ConfirmationInput:
    kind: str
    version: str
    language: str
    confirmed: bool


def _work_kind(draft: NoticeDraft) -> str:
    """`video` or `photos` for the work this draft is about (`matching.Reference.kind`).

    Read from the references rather than snapshotted on the draft, which needs no column and is
    just as stable: a digest's kind is fixed for ever — a collection digest is domain-separated
    from a file's sha256 (ADR 0020), so one sha256 can never change shape. A draft whose references
    are gone, or a machine that holds none (production answers notices from the signed cache,
    ADR 0046), falls back to the video wording, which is what every draft before issue #116 was.
    """
    kind = (
        Reference.objects.filter(sha256=draft.sha256)
        .values_list("kind", flat=True)
        .order_by("pk")
        .first()
    )
    return str(kind) if kind else KIND_VIDEO


def _context_of(draft: NoticeDraft, legal_paragraph: str) -> NoticeContext:
    findings = tuple(
        NoticeFindingFact(
            url=row.url_snapshot,
            first_seen=_as_date(row.first_seen_at),
            last_checked=row.last_checked_at,
            reviewed_on=_as_date(row.reviewed_at),
        )
        for row in draft.findings.filter(included=True).order_by("pk")
    )
    return NoticeContext(
        language=draft.language,
        claimant_name=draft.claimant_name,
        claimant_capacity=draft.claimant_capacity,
        underlying_owner_name=draft.underlying_owner_name,
        claimant_email=draft.claimant_email,
        claimant_address=draft.claimant_address,
        claimant_phone=draft.claimant_phone,
        work_title=draft.work_title,
        work_id=draft.original_public_id,
        work_kind=_work_kind(draft),
        legitimate_source_url=draft.legitimate_source_url,
        publication_date=draft.publication_date,
        findings=findings,
        recipient_name=draft.recipient_provider_name or draft.recipient_hostname,
        recipient_email=draft.recipient_email,
        recipient_url=draft.recipient_url,
        legal_paragraph=legal_paragraph,
    )


def _as_date(value: datetime | None) -> date | None:
    return value.date() if value is not None else None


def _legal_paragraph(language: str) -> str:
    """The customer's own legal paragraph: rights + authorisation + exceptions, joined."""
    return " ".join(
        declarations.current(kind).text(language)
        for kind in ("rights", "authorisation", "exceptions")
    )


def render_preview(draft: NoticeDraft) -> str:
    """The current live render, for the screen before approval — never stored."""
    return render(draft.rendered_variant, _context_of(draft, _legal_paragraph(draft.language)))


@transaction.atomic
def approve(
    draft: NoticeDraft,
    confirmations: Sequence[ConfirmationInput],
    actor: Actor,
    *,
    now: datetime | None = None,
) -> NoticeDraft:
    """Freeze the exact rendered text the customer confirmed, and record every confirmation.

    Refuses unless the draft is still `draft`, every included finding is still eligible (the
    world can have changed since creation — a page can go offline, a right can be disputed), and
    every required confirmation is present, current-versioned and ticked.
    """
    now = now or timezone.now()
    locked = NoticeDraft.objects.select_for_update().get(pk=draft.pk)
    if locked.status != NoticeStatus.DRAFT:
        raise NoticeError("not_draft")
    _require_visible(locked)
    included = list(locked.findings.filter(included=True).select_related("page__site"))
    if not included:
        raise NoticeError("no_findings_included")
    reference = _reference_of(locked)
    if reference is None:
        raise NoticeError("reference_missing")
    matches = file_matches(locked.organisation, locked.sha256, [row.page_id for row in included])
    blockers_by_page = {}
    for row in included:
        blockers = finding_gate_blockers(
            locked.organisation,
            reference,
            row.page,
            matches.get(row.page_id),
            exception_confirmed=row.exception_confirmed,
            now=now,
        )
        if blockers:
            blockers_by_page[row.page.public_id] = blockers
    if blockers_by_page:
        raise NoticeError("finding_no_longer_eligible", blockers_by_page)

    by_kind = {item.kind: item for item in confirmations}
    missing_kinds = []
    for kind in required_confirmations(locked):
        item = by_kind.get(kind)
        current = declarations.current(kind)
        if (
            item is None
            or not item.confirmed
            or item.version != current.version
            or item.language not in declarations.LANGUAGES
        ):
            missing_kinds.append(kind)
    if missing_kinds:
        raise NoticeError("confirmation_incomplete", missing_kinds)

    text = render(locked.rendered_variant, _context_of(locked, _legal_paragraph(locked.language)))
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    for kind in required_confirmations(locked):
        item = by_kind[kind]
        current = declarations.current(kind)
        NoticeConfirmation.objects.update_or_create(
            draft=locked,
            kind=kind,
            defaults={
                "version": current.version,
                "language": item.language,
                "text": current.text(item.language),
                "confirmed_at": now,
                "confirmed_by_id": actor.user_id,
                "confirmed_by_label": actor.label,
            },
        )
    locked.approved_text = text
    locked.approved_text_sha256 = text_hash
    locked.approved_at = now
    locked.approved_by = actor.account
    locked.status = NoticeStatus.CUSTOMER_APPROVED
    locked.save(
        update_fields=[
            "approved_text",
            "approved_text_sha256",
            "approved_at",
            "approved_by",
            "status",
            "updated_at",
        ]
    )
    _log(
        locked,
        NoticeEventKind.APPROVED,
        actor,
        detail={
            "sha256": text_hash,
            "language": locked.language,
            "variant": locked.rendered_variant,
        },
    )
    return locked


def _annex_context_of(draft: NoticeDraft) -> export.AnnexContext:
    rows = tuple(
        export.AnnexRow(
            url=row.url_snapshot,
            first_seen=_as_date(row.first_seen_at),
            last_checked=row.last_checked_at,
            reviewed_on=_as_date(row.reviewed_at),
            extent=row.extent_snapshot,
            # Only a copy that needed the per-finding exception review says anything about it;
            # for the rest the question was never asked and the annex stays silent.
            exception_reviewed=row.exception_confirmed if row.requires_exception_review else None,
        )
        for row in draft.findings.filter(included=True).order_by("pk")
    )
    return export.AnnexContext(
        language=draft.language,
        notice_id=draft.public_id,
        work_title=draft.work_title,
        work_id=draft.original_public_id,
        work_kind=_work_kind(draft),
        legitimate_source_url=draft.legitimate_source_url,
        recipient_name=draft.recipient_provider_name or draft.recipient_hostname,
        recipient_hostname=draft.recipient_hostname,
        approved_text_sha256=draft.approved_text_sha256,
        rows=rows,
    )


def export_payload(draft: NoticeDraft, fmt: str) -> export.ExportFile:
    """Format one download — and nothing else: no event, no lock, no transport.

    The one formatter, so the bytes are the same wherever they are produced: development's own
    answer, the copy the signed bundle carries for production (ADR 0046, `marceline/export.py`)
    and `export_notice` below all come through here. The refusals are the same too, so a draft
    that is not exportable answers the same 409 on either machine.
    """
    if fmt not in export.FORMATS:
        raise NoticeError("unknown_format")
    # The text is about to leave this machine: re-read every reported page first, exactly as
    # `sandbox_send` does.
    _require_visible(draft)
    if draft.status in CLOSED_NOTICE_STATUSES:
        raise NoticeError("already_closed")
    if not draft.approved_text:
        raise NoticeError("not_approved")
    if fmt == "txt":
        return export.text_file(draft.public_id, draft.approved_text)
    if fmt == "eml":
        subject, body = split_subject(draft.approved_text)
        return export.eml_file(
            draft.public_id,
            subject=subject or draft.work_title,
            body=body,
            to=draft.recipient_email,
        )
    return export.annex_file(draft.public_id, _annex_context_of(draft))


@transaction.atomic
def export_notice(draft: NoticeDraft, fmt: str, actor: Actor) -> export.ExportFile:
    """The approved text as a file the customer keeps (ADR 0035 amendment 2026-09-18).

    Sends nothing: it formats text that is already frozen on the draft and appends one
    `exported` event. The customer may copy the text, open it in their own mail program or
    download it — this is the download, and the `.eml` format is the
    "open it in your own mail program" that a `mailto:` link cannot carry for a long notice.

    Refused unless the draft is still one the customer may see at all (`_require_visible`, so a
    newly safety-flagged page stops every export of its draft the same moment it stops the
    screen), has been approved (`not_approved` — the export must never be a way around the
    confirmations), and is not closed (`already_closed`: a withdrawn or corrected notice is not a
    notice to send, and its text stays readable in the record instead).
    """
    locked = NoticeDraft.objects.select_for_update().get(pk=draft.pk)
    file = export_payload(locked, fmt)
    _log(
        locked,
        NoticeEventKind.EXPORTED,
        actor,
        detail={
            "format": fmt,
            # The checksum of what was approved, so the log says *which* text left — never a
            # second copy of the text itself.
            "sha256": locked.approved_text_sha256,
            "language": locked.language,
            "variant": locked.rendered_variant,
        },
    )
    return file


@transaction.atomic
def mark_sent(draft: NoticeDraft, actor: Actor, *, now: datetime | None = None) -> NoticeDraft:
    """The customer's own "I sent this myself". Never for a sandbox organisation."""
    now = now or timezone.now()
    locked = NoticeDraft.objects.select_for_update().get(pk=draft.pk)
    if locked.is_sandbox:
        raise NoticeError("sandbox_cannot_mark_real_send")
    if locked.status != NoticeStatus.CUSTOMER_APPROVED:
        raise NoticeError("not_approved")
    locked.status = NoticeStatus.MARKED_SENT_BY_CUSTOMER
    locked.marked_sent_at = now
    locked.marked_sent_by = actor.account
    locked.save(update_fields=["status", "marked_sent_at", "marked_sent_by", "updated_at"])
    # From here the notice is out in the world: open its cooperation row so the pages it reports
    # are watched and a removal can be observed even if the provider never answers.
    contact = _open_cooperation(locked)
    _attach_cooperation(locked, contact)
    _log(
        locked,
        NoticeEventKind.MARKED_SENT,
        actor,
        detail={"cooperation_opened": contact is not None},
    )
    return locked


def sandbox_recipients() -> tuple[str, ...]:
    return tuple(settings.NOTICE_SANDBOX_RECIPIENTS)


@transaction.atomic
def sandbox_send(
    draft: NoticeDraft, actor: Actor, recipient_email: str, *, now: datetime | None = None
) -> NoticeDraft:
    """The only place Marceline may send an email copy of a draft: to an allowlisted test
    mailbox, for a sandbox organisation's draft only. Any other address is hard-refused."""
    now = now or timezone.now()
    locked = NoticeDraft.objects.select_for_update().get(pk=draft.pk)
    # The only path that moves a draft's text off this machine: re-read every page first.
    _require_visible(locked)
    if not locked.is_sandbox:
        raise NoticeError("not_sandbox")
    if locked.status != NoticeStatus.CUSTOMER_APPROVED:
        raise NoticeError("not_approved")
    if recipient_email not in sandbox_recipients():
        raise NoticeError("recipient_not_allowlisted")
    message = EmailMessage(
        subject=f"[Marceline sandbox] {locked.rendered_variant}: {locked.work_title}",
        body=locked.approved_text,
        from_email=settings.NOTICE_SANDBOX_FROM or None,
        to=[recipient_email],
    )
    message.send(fail_silently=False)
    locked.status = NoticeStatus.MARKED_SENT_BY_CUSTOMER
    locked.marked_sent_at = now
    locked.marked_sent_by = actor.account
    locked.save(update_fields=["status", "marked_sent_at", "marked_sent_by", "updated_at"])
    _log(locked, NoticeEventKind.SANDBOX_SENT, actor, detail={"to": recipient_email})
    return locked


def _channel_for(draft: NoticeDraft) -> str:
    if draft.recipient_email:
        return HosterContact.Channel.EMAIL
    if draft.recipient_url:
        return HosterContact.Channel.WEB_FORM
    return HosterContact.Channel.OTHER


def _open_cooperation(draft: NoticeDraft) -> HosterContact | None:
    """The cooperation-history row of this notice, opened at send time if it has none yet.

    A row exists from the moment a **real** notice goes out, with `outcome=awaiting` and the pages
    it reports linked — not only once a provider answers. That is what lets
    `marceline.hosting.observe_notice_removals` stamp `removed_observed_at` from those pages' own
    availability checks: the common case is a provider that removes the copy and never writes back,
    and before this the loop simply never closed for it. `catalog.hosting.speed` and `.cooperation`
    already read `awaiting` correctly — a young open notice is neither a success nor a failure, and
    it only counts as "never" once it is older than `OPEN_NOTICE_WINDOW`.

    Never for a sandbox draft, so a test notice can never move a real provider's statistics, and
    never twice for the same draft.
    """
    if draft.is_sandbox:
        return None
    if draft.hoster_contact_id is not None:
        return draft.hoster_contact
    record = HostingRecord.objects.filter(hostname=draft.recipient_hostname).first()
    if record is None or record.provider_id is None:
        return None
    contact = HosterContact.objects.create(
        provider_id=record.provider_id,
        hostname=draft.recipient_hostname,
        channel=_channel_for(draft),
        sent_at=draft.marked_sent_at or draft.responded_at or timezone.now(),
        outcome=HosterContact.Outcome.AWAITING,
    )
    pages = [row.page for row in draft.findings.filter(included=True)]
    contact.pages.set(pages)
    return contact


def _settle_cooperation(draft: NoticeDraft) -> HosterContact | None:
    """Write what the provider did onto this notice's cooperation row (opening it if the notice
    predates `_open_cooperation`). `removed_observed_at` is never touched here — it is stamped from
    availability checks alone."""
    contact = _open_cooperation(draft)
    if contact is None:
        return None
    contact.responded_at = draft.responded_at
    contact.outcome = draft.outcome or HosterContact.Outcome.AWAITING
    contact.save(update_fields=["responded_at", "outcome"])
    return contact


def _attach_cooperation(draft: NoticeDraft, contact: HosterContact | None) -> None:
    if contact is not None and draft.hoster_contact_id != contact.pk:
        draft.hoster_contact = contact
        draft.save(update_fields=["hoster_contact"])


@transaction.atomic
def record_provider_response(
    draft: NoticeDraft, outcome: str, actor: Actor, *, responded_at: datetime | None = None
) -> NoticeDraft:
    """The customer reports what the provider did. Settles this notice's cooperation row, which
    `mark_sent` opened — a sandbox draft has none and never gets one."""
    if outcome not in ProviderResponseOutcome.values:
        raise NoticeError("unknown_outcome")
    now = timezone.now()
    locked = NoticeDraft.objects.select_for_update().get(pk=draft.pk)
    if locked.status != NoticeStatus.MARKED_SENT_BY_CUSTOMER:
        raise NoticeError("not_sent")
    locked.status = NoticeStatus.PROVIDER_RESPONDED
    locked.outcome = outcome
    locked.responded_at = responded_at or now
    locked.save(update_fields=["status", "outcome", "responded_at", "updated_at"])
    contact = _settle_cooperation(locked)
    _attach_cooperation(locked, contact)
    _log(
        locked,
        NoticeEventKind.PROVIDER_RESPONSE_RECORDED,
        actor,
        detail={"outcome": outcome, "cooperation_recorded": contact is not None},
    )
    return locked


@transaction.atomic
def withdraw(draft: NoticeDraft, reason: str, note: str, actor: Actor) -> NoticeDraft:
    """Close an open draft. A withdrawn notice stops counting towards its provider's history.

    Since `mark_sent` opens the cooperation row, a notice withdrawn after it went out would
    otherwise sit in `catalog.HosterContact` and, once older than `OPEN_NOTICE_WINDOW`, count as
    "never removed" against a provider the customer had told to disregard it — a mistaken match
    (`mistaken_match`) is the customer's error, not the provider's. `HosterContact` is the
    statistics feed, not the audit trail: what happened stays in the append-only `NoticeEvent` log
    and on the draft, both of which are kept.
    """
    if reason not in declarations.WITHDRAWAL_REASONS:
        raise NoticeError("unknown_reason")
    now = timezone.now()
    locked = NoticeDraft.objects.select_for_update().get(pk=draft.pk)
    if locked.status not in OPEN_STATUSES:
        raise NoticeError("already_closed")
    locked.status = NoticeStatus.WITHDRAWN
    locked.withdrawn_at = now
    locked.withdrawal_reason = reason
    contact = locked.hoster_contact
    locked.hoster_contact = None
    locked.save(
        update_fields=[
            "status",
            "withdrawn_at",
            "withdrawal_reason",
            "hoster_contact",
            "updated_at",
        ]
    )
    if contact is not None:
        contact.delete()
    _log(
        locked,
        NoticeEventKind.WITHDRAWN,
        actor,
        detail={
            "reason": reason,
            "note": note[:500],
            "cooperation_withdrawn": contact is not None,
        },
    )
    return locked


@transaction.atomic
def correct(draft: NoticeDraft, reason: str, note: str, actor: Actor) -> NoticeDraft:
    """Close this draft as `corrected` and open a fresh `draft` copy of its findings and facts,
    so every confirmation has to be given again for the corrected version."""
    if reason not in declarations.CORRECTION_REASONS:
        raise NoticeError("unknown_reason")
    now = timezone.now()
    locked = NoticeDraft.objects.select_for_update().get(pk=draft.pk)
    # The successor copies the findings, so it may not be opened on a page that is now hidden.
    _require_visible(locked)
    if locked.status not in OPEN_STATUSES:
        raise NoticeError("already_closed")
    successor = NoticeDraft.objects.create(
        organisation=locked.organisation,
        is_sandbox=locked.is_sandbox,
        language=locked.language,
        rendered_variant=locked.rendered_variant,
        recipient_hostname=locked.recipient_hostname,
        recipient_provider_name=locked.recipient_provider_name,
        channel_kind=locked.channel_kind,
        recipient_email=locked.recipient_email,
        recipient_url=locked.recipient_url,
        complaint_channel_id=locked.complaint_channel_id,
        forwards_to_site_operator=locked.forwards_to_site_operator,
        publishes_to_transparency_db=locked.publishes_to_transparency_db,
        sha256=locked.sha256,
        original_public_id=locked.original_public_id,
        work_title=locked.work_title,
        legitimate_source_url=locked.legitimate_source_url,
        publication_date=locked.publication_date,
        claimant_name=locked.claimant_name,
        claimant_capacity=locked.claimant_capacity,
        underlying_owner_name=locked.underlying_owner_name,
        claimant_email=locked.claimant_email,
        claimant_address=locked.claimant_address,
        claimant_phone=locked.claimant_phone,
        asserted_right=locked.asserted_right,
        asserted_right_detail=locked.asserted_right_detail,
        created_at=now,
    )
    for row in locked.findings.all():
        NoticeFinding.objects.create(
            draft=successor,
            page=row.page,
            url_snapshot=row.url_snapshot,
            first_seen_at=row.first_seen_at,
            last_checked_at=row.last_checked_at,
            reviewed_at=row.reviewed_at,
            availability_snapshot=row.availability_snapshot,
            extent_snapshot=row.extent_snapshot,
            requires_exception_review=row.requires_exception_review,
            included=row.included,
        )
    locked.status = NoticeStatus.CORRECTED
    locked.correction_note = note[:2000]
    locked.corrected_into = successor
    locked.save(update_fields=["status", "correction_note", "corrected_into", "updated_at"])
    _log(
        locked,
        NoticeEventKind.CORRECTED,
        actor,
        detail={"reason": reason, "successor": successor.pk},
    )
    _log(successor, NoticeEventKind.CREATED, actor, detail={"corrects": locked.pk})
    return successor


def unknown_notice_public_id() -> str:
    return missing("notice")
