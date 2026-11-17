"""`marceline.notices.services`: the only writer of notice drafts (issue #33, ADR 0035).

Covers what the build spec calls out by name: confirmation versioning, the append-only event log,
sandbox refusal of a non-allowlisted address, and sandbox/test-organisation exclusion from the
provider response history (`catalog.HosterContact`) — plus the loop that closes it: a real notice
opens its cooperation row when the customer marks it sent, so an ordinary availability check can
observe the removal even though the provider never wrote back.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.db import IntegrityError, transaction
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from catalog.models import HosterContact
from index.models import Page
from marceline.hosting import observe_notice_removals
from marceline.notices import declarations, services
from marceline.notices.models import NoticeDraft, NoticeEvent, NoticeStatus
from marceline.tests.app_helpers import add_member, make_organisation, make_reference, own
from marceline.tests.notices_helpers import (
    all_confirmations,
    claimant_input,
    make_hosting_record,
    playable,
    recipient_choice,
    rights_ready,
)
from marceline.tests.test_app_originals import match
from matching.fingerprint import PIPELINE, PIPELINE_V0
from matching.models import Reference, ReferenceMatch

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "alpha-owner")


@pytest.fixture
def staff(db: None) -> User:
    return User.objects.create_user("reviewer", password="secret-pass-123", is_staff=True)


def _ready_finding(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> tuple[Reference, Page, ReferenceMatch]:
    reference = make_reference(member)
    page = playable(make_page("candidate"))
    make_hosting_record(page.site.domain)
    row = match(
        reference,
        page,
        band="high",
        status="confirmed",
        copy_extent="full_copy",
        copy_extent_version=1,
    )
    rights_ready(organisation, reference.sha256, member, staff)
    return reference, page, row


def _draft(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> NoticeDraft:
    reference, page, _ = _ready_finding(organisation, member, staff, make_page)
    return services.create_draft(
        organisation,
        reference,
        [page.pk],
        recipient_choice(hostname=page.site.domain),
        "dsa_art16",
        "en",
        claimant_input(),
        work_title=reference.title,
        original_public_id="orig_deadbeefdeadbeefdeadbeefdeadbeef",
        actor=services.Actor.customer(member),
        now=NOW,
    )


def test_create_draft_refuses_an_ineligible_finding(
    organisation: Organisation, member: User, make_page: Callable[[str], Page]
) -> None:
    reference = make_reference(member)
    page = playable(make_page("candidate"))
    # No rights record, no hosting record: everything is missing.

    with pytest.raises(services.NoticeError) as error:
        services.create_draft(
            organisation,
            reference,
            [page.pk],
            recipient_choice(),
            "dsa_art16",
            "en",
            claimant_input(),
            work_title=reference.title,
            original_public_id="orig_deadbeefdeadbeefdeadbeefdeadbeef",
            actor=services.Actor.customer(member),
            now=NOW,
        )

    assert error.value.code == "finding_not_eligible"
    assert page.public_id in error.value.detail


def test_create_draft_needs_the_per_finding_exception_tick_for_an_excerpt(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    reference = make_reference(member)
    page = playable(make_page("candidate"))
    make_hosting_record(page.site.domain)
    match(
        reference,
        page,
        band="high",
        status="confirmed",
        copy_extent="excerpt",
        copy_extent_version=1,
    )
    rights_ready(organisation, reference.sha256, member, staff)

    with pytest.raises(services.NoticeError) as error:
        services.create_draft(
            organisation,
            reference,
            [page.pk],
            recipient_choice(hostname=page.site.domain),
            "dsa_art16",
            "en",
            claimant_input(),
            work_title=reference.title,
            original_public_id="orig_deadbeefdeadbeefdeadbeefdeadbeef",
            actor=services.Actor.customer(member),
            now=NOW,
        )
    assert error.value.code == "finding_not_eligible"
    assert "exception_review_required" in error.value.detail[page.public_id]

    draft = services.create_draft(
        organisation,
        reference,
        [page.pk],
        recipient_choice(hostname=page.site.domain),
        "dsa_art16",
        "en",
        claimant_input(),
        work_title=reference.title,
        original_public_id="orig_deadbeefdeadbeefdeadbeefdeadbeef",
        exception_confirmed_pages=frozenset({page.pk}),
        actor=services.Actor.customer(member),
        now=NOW,
    )
    assert draft.findings.get().exception_confirmed is True


def test_approve_refuses_without_every_required_confirmation(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    draft = _draft(organisation, member, staff, make_page)
    incomplete = [item for item in all_confirmations() if item.kind != "accuracy"]

    with pytest.raises(services.NoticeError) as error:
        services.approve(draft, incomplete, services.Actor.customer(member), now=NOW)

    assert error.value.code == "confirmation_incomplete"
    assert "accuracy" in error.value.detail


def test_approve_refuses_an_outdated_confirmation_version(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    draft = _draft(organisation, member, staff, make_page)
    stale = all_confirmations(version_overrides={"rights": "notice-rights-2020-01-01"})

    with pytest.raises(services.NoticeError) as error:
        services.approve(draft, stale, services.Actor.customer(member), now=NOW)

    assert error.value.code == "confirmation_incomplete"
    assert "rights" in error.value.detail


def test_approve_stores_confirmations_verbatim_and_freezes_the_text(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    draft = _draft(organisation, member, staff, make_page)

    approved = services.approve(
        draft, all_confirmations(), services.Actor.customer(member), now=NOW
    )

    assert approved.status == NoticeStatus.CUSTOMER_APPROVED
    assert approved.approved_text
    assert approved.approved_text_sha256
    stored = {row.kind: row for row in approved.confirmations.all()}
    assert stored["rights"].text == declarations.current("rights").text("en")
    assert stored["rights"].version == declarations.current("rights").version
    event = approved.events.get(kind="approved")
    assert event.detail["sha256"] == approved.approved_text_sha256


def test_the_event_log_is_append_only_in_the_database(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    draft = _draft(organisation, member, staff, make_page)
    event = NoticeEvent.objects.filter(draft=draft, kind="created").get()

    with pytest.raises(IntegrityError), transaction.atomic():
        NoticeEvent.objects.filter(pk=event.pk).update(actor_label="someone else")


def test_mark_sent_is_refused_for_a_sandbox_organisation(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    organisation.is_sandbox = True
    organisation.save(update_fields=["is_sandbox"])
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)

    with pytest.raises(services.NoticeError) as error:
        services.mark_sent(draft, services.Actor.customer(member), now=NOW)

    assert error.value.code == "sandbox_cannot_mark_real_send"


def test_sandbox_send_refuses_a_non_allowlisted_address(
    organisation: Organisation,
    member: User,
    staff: User,
    make_page: Callable[[str], Page],
    settings: Settings,
) -> None:
    settings.NOTICE_SANDBOX_RECIPIENTS = ["sandbox@marceline.test"]
    organisation.is_sandbox = True
    organisation.save(update_fields=["is_sandbox"])
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)

    with pytest.raises(services.NoticeError) as error:
        services.sandbox_send(
            draft, services.Actor.customer(member), "attacker@real-provider.example", now=NOW
        )

    assert error.value.code == "recipient_not_allowlisted"
    assert len(mail.outbox) == 0


def test_the_unconfigured_default_can_send_to_nobody_at_all(
    organisation: Organisation,
    member: User,
    staff: User,
    make_page: Callable[[str], Page],
) -> None:
    """The configuration a test run (and a fresh install) has is the empty allowlist.

    `conftest.OPERATIONAL_DEFAULTS` pins `NOTICE_SANDBOX_RECIPIENTS` to `[]`, so no test can reach
    the operator's real mailbox by accident — and with no allowlist there is no address the send
    path accepts, whatever the caller asks for. This is the standing guarantee that a test run
    cannot put a notice in front of a provider, an abuse contact or anyone else.
    """
    assert services.sandbox_recipients() == ()
    organisation.is_sandbox = True
    organisation.save(update_fields=["is_sandbox"])
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)

    for address in ("abuse@hosting.example", "sandbox@marceline.test", ""):
        with pytest.raises(services.NoticeError) as error:
            services.sandbox_send(draft, services.Actor.customer(member), address, now=NOW)
        assert error.value.code == "recipient_not_allowlisted"

    assert len(mail.outbox) == 0
    draft.refresh_from_db()
    assert draft.status == NoticeStatus.CUSTOMER_APPROVED


def test_sandbox_send_refuses_for_a_non_sandbox_organisation(
    organisation: Organisation,
    member: User,
    staff: User,
    make_page: Callable[[str], Page],
    settings: Settings,
) -> None:
    settings.NOTICE_SANDBOX_RECIPIENTS = ["sandbox@marceline.test"]
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)

    with pytest.raises(services.NoticeError) as error:
        services.sandbox_send(
            draft, services.Actor.customer(member), "sandbox@marceline.test", now=NOW
        )

    assert error.value.code == "not_sandbox"


def test_sandbox_send_to_an_allowlisted_address_marks_sent_and_sends_one_mail(
    organisation: Organisation,
    member: User,
    staff: User,
    make_page: Callable[[str], Page],
    settings: Settings,
) -> None:
    settings.NOTICE_SANDBOX_RECIPIENTS = ["sandbox@marceline.test"]
    organisation.is_sandbox = True
    organisation.save(update_fields=["is_sandbox"])
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)

    sent = services.sandbox_send(
        draft, services.Actor.customer(member), "sandbox@marceline.test", now=NOW
    )

    assert sent.status == NoticeStatus.MARKED_SENT_BY_CUSTOMER
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["sandbox@marceline.test"]


def test_marking_a_real_notice_sent_opens_its_cooperation_row_awaiting_an_answer(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    """The common case is a provider that removes the copy and never writes back, so the row —
    and the page links `observe_notice_removals` reads — exists from the send, not from a reply."""
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)

    sent = services.mark_sent(draft, services.Actor.customer(member), now=NOW)

    contact = sent.hoster_contact
    assert contact is not None
    assert contact.outcome == HosterContact.Outcome.AWAITING
    assert contact.sent_at == NOW
    assert contact.responded_at is None
    assert contact.removed_observed_at is None
    reported = {row.page_id for row in sent.findings.filter(included=True)}
    assert {page.pk for page in contact.pages.all()} == reported


def test_a_sent_notice_nobody_answered_is_still_observed_when_its_page_goes(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    """The loop the operator asked for, end to end: the customer marks a notice sent, the page is
    later found gone by an ordinary availability check, and the notice carries the stamp — with no
    provider reply anywhere in it."""
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)
    sent = services.mark_sent(draft, services.Actor.customer(member), now=NOW)
    gone_at = NOW + timedelta(days=2)
    for row in sent.findings.all():
        page = row.page
        page.availability = Page.Availability.REMOVED
        page.availability_checked_at = gone_at
        page.save(update_fields=["availability", "availability_checked_at"])

    assert observe_notice_removals() == 1

    sent.refresh_from_db()
    assert sent.outcome == ""  # nothing came back; the customer reported nothing
    assert sent.hoster_contact is not None
    assert sent.hoster_contact.removed_observed_at == gone_at


def test_a_real_organisations_provider_response_settles_its_one_cooperation_row(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)
    sent = services.mark_sent(draft, services.Actor.customer(member), now=NOW)
    opened = sent.hoster_contact
    assert opened is not None

    responded = services.record_provider_response(
        draft, "removed", services.Actor.customer(member), responded_at=NOW
    )

    contact = responded.hoster_contact
    assert contact is not None
    # The same row, settled — never a second one, which would count the notice twice.
    assert contact.pk == opened.pk
    assert HosterContact.objects.count() == 1
    contact.refresh_from_db()
    assert contact.outcome == "removed"
    assert contact.responded_at == NOW


def test_a_notice_sent_before_the_row_existed_still_gets_one_on_a_response(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    """Drafts marked sent before this change carry no cooperation row; a response opens it."""
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)
    sent = services.mark_sent(draft, services.Actor.customer(member), now=NOW)
    HosterContact.objects.all().delete()
    NoticeDraft.objects.filter(pk=sent.pk).update(hoster_contact=None)

    responded = services.record_provider_response(
        NoticeDraft.objects.get(pk=sent.pk),
        "refused",
        services.Actor.customer(member),
        responded_at=NOW,
    )

    assert responded.hoster_contact is not None
    assert responded.hoster_contact.outcome == "refused"


def test_a_sandbox_notice_never_opens_a_cooperation_row_when_it_is_sent(
    organisation: Organisation,
    member: User,
    staff: User,
    make_page: Callable[[str], Page],
    settings: Settings,
) -> None:
    settings.NOTICE_SANDBOX_RECIPIENTS = ["sandbox@marceline.test"]
    organisation.is_sandbox = True
    organisation.save(update_fields=["is_sandbox"])
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)

    sent = services.sandbox_send(
        draft, services.Actor.customer(member), "sandbox@marceline.test", now=NOW
    )

    assert sent.hoster_contact is None
    assert HosterContact.objects.count() == 0


def test_a_sandbox_organisations_provider_response_never_touches_cooperation_history(
    organisation: Organisation,
    member: User,
    staff: User,
    make_page: Callable[[str], Page],
    settings: Settings,
) -> None:
    settings.NOTICE_SANDBOX_RECIPIENTS = ["sandbox@marceline.test"]
    organisation.is_sandbox = True
    organisation.save(update_fields=["is_sandbox"])
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)
    services.sandbox_send(draft, services.Actor.customer(member), "sandbox@marceline.test", now=NOW)

    before = HosterContact.objects.count()
    responded = services.record_provider_response(
        draft, "removed", services.Actor.customer(member), responded_at=NOW
    )

    assert responded.hoster_contact is None
    assert HosterContact.objects.count() == before


def test_withdrawing_a_sent_notice_takes_it_out_of_the_provider_history(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    """A notice the customer took back must not later count as "never removed" against the
    provider they told to disregard it. The event log keeps what happened."""
    draft = _draft(organisation, member, staff, make_page)
    services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)
    sent = services.mark_sent(draft, services.Actor.customer(member), now=NOW)
    assert sent.hoster_contact is not None

    withdrawn = services.withdraw(sent, "mistaken_match", "", services.Actor.customer(member))

    assert withdrawn.hoster_contact is None
    assert HosterContact.objects.count() == 0
    event = withdrawn.events.get(kind="withdrawn")
    assert event.detail["cooperation_withdrawn"] is True


def test_withdraw_then_refuses_a_second_close(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    draft = _draft(organisation, member, staff, make_page)

    withdrawn = services.withdraw(draft, "mistaken_match", "", services.Actor.customer(member))
    assert withdrawn.status == NoticeStatus.WITHDRAWN

    with pytest.raises(services.NoticeError) as error:
        services.withdraw(withdrawn, "mistaken_match", "", services.Actor.customer(member))
    assert error.value.code == "already_closed"


def test_correct_opens_a_successor_with_the_same_findings(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    draft = _draft(organisation, member, staff, make_page)

    successor = services.correct(draft, "wrong_recipient", "typo", services.Actor.customer(member))

    draft.refresh_from_db()
    assert draft.status == NoticeStatus.CORRECTED
    assert draft.corrected_into_id == successor.pk
    assert successor.status == NoticeStatus.DRAFT
    assert list(successor.findings.values_list("page_id", flat=True)) == list(
        draft.findings.values_list("page_id", flat=True)
    )
    # A correction needs re-confirmation: nothing carries over from the closed draft.
    assert successor.confirmations.count() == 0


def test_approve_lets_a_rejection_on_any_reference_of_the_file_veto(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    """A re-fingerprinted file has several references (ADR 0022), one per recipe and rate.

    `approve` reads all of them through `gate.file_matches` — a confirmation on any counts, which
    is what the customer is shown — but a **rejected** row on any of them vetoes: it is this
    gate's one human review step, and no sampling grid may stand in for a decision somebody took
    against it. Before, the answer depended on which row the query returned first.
    """
    reference, page, row = _ready_finding(organisation, member, staff, make_page)
    # The organisation's representative is the one on the current recipe (ADR 0022).
    reference.pipeline = PIPELINE
    reference.save(update_fields=["pipeline"])
    draft = services.create_draft(
        organisation,
        reference,
        [page.pk],
        recipient_choice(hostname=page.site.domain),
        "dsa_art16",
        "en",
        claimant_input(),
        work_title=reference.title,
        original_public_id="orig_deadbeefdeadbeefdeadbeefdeadbeef",
        actor=services.Actor.customer(member),
        now=NOW,
    )
    twin = own(
        Reference.objects.create(
            creator=reference.creator,
            title=reference.title,
            duration_s=reference.duration_s,
            sha256=reference.sha256,
            frame_fps=reference.frame_fps,
            pipeline=PIPELINE_V0,
        ),
        organisation,
    )
    match(
        twin,
        page,
        band="high",
        status="confirmed",
        copy_extent="full_copy",
        copy_extent_version=1,
    )
    row.status = ReferenceMatch.Status.REJECTED
    row.save(update_fields=["status"])

    with pytest.raises(services.NoticeError) as error:
        services.approve(draft, all_confirmations(), services.Actor.customer(member), now=NOW)

    assert error.value.code == "finding_no_longer_eligible"
    assert error.value.detail == {page.public_id: ["no_high_confirmed_match"]}


def test_create_draft_refuses_a_safety_flagged_page(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    """The writer refuses it too, not only the endpoint's candidate list (ADR 0011, ADR 0018).

    A flagged page is invisible to the customer everywhere; a draft is the one record that would
    otherwise keep its URL in a snapshot of its own.
    """
    reference, page, _ = _ready_finding(organisation, member, staff, make_page)
    page.safety_status = Page.SafetyStatus.BLOCKED
    page.safety_matches = ["a term"]
    page.save(update_fields=["safety_status", "safety_matches"])

    with pytest.raises(services.NoticeError) as error:
        services.create_draft(
            organisation,
            reference,
            [page.pk],
            recipient_choice(hostname=page.site.domain),
            "dsa_art16",
            "en",
            claimant_input(),
            work_title=reference.title,
            original_public_id="orig_deadbeefdeadbeefdeadbeefdeadbeef",
            actor=services.Actor.customer(member),
            now=NOW,
        )

    assert error.value.code == "unknown_page"


# --- the human-review date (issue #142) -----------------------------------------------------------


def test_the_draft_snapshots_the_confirming_decisions_date(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    """`NoticeFinding.reviewed_at` is a snapshot, like every other field on that row.

    `approve` freezes a sha256 over the rendered text, so the text must not depend on data that can
    move afterwards — and `ReferenceMatch.updated_at` moves whenever anything touches the row.
    """
    reference, page, row = _ready_finding(organisation, member, staff, make_page)
    decided = NOW - timedelta(days=7)
    ReferenceMatch.objects.filter(pk=row.pk).update(decided_at=decided)

    draft = services.create_draft(
        organisation,
        reference,
        [page.pk],
        recipient_choice(hostname=page.site.domain),
        "dsa_art16",
        "en",
        claimant_input(),
        work_title=reference.title,
        original_public_id="orig_deadbeefdeadbeefdeadbeefdeadbeef",
        actor=services.Actor.customer(member),
        now=NOW,
    )

    assert draft.findings.get().reviewed_at == decided
    # And it is in the text the customer would send.
    assert decided.date().isoformat() in services.render_preview(draft)

    # A later touch of the match row does not move the draft's date.
    ReferenceMatch.objects.filter(pk=row.pk).update(decided_at=NOW)
    assert draft.findings.get().reviewed_at == decided


def test_a_correction_carries_the_same_review_date(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    reference, page, row = _ready_finding(organisation, member, staff, make_page)
    decided = NOW - timedelta(days=3)
    ReferenceMatch.objects.filter(pk=row.pk).update(decided_at=decided)
    draft = services.create_draft(
        organisation,
        reference,
        [page.pk],
        recipient_choice(hostname=page.site.domain),
        "dsa_art16",
        "en",
        claimant_input(),
        work_title=reference.title,
        original_public_id="orig_deadbeefdeadbeefdeadbeefdeadbeef",
        actor=services.Actor.customer(member),
        now=NOW,
    )

    successor = services.correct(
        draft, "factual_error", "a date was wrong", services.Actor.customer(member)
    )

    assert successor.findings.get().reviewed_at == decided


def test_a_finding_nobody_confirmed_cannot_enter_a_draft_at_all(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> None:
    """Which is why a draft's review date can only ever be missing for a decision taken before the
    column existed: the gate refuses `no_high_confirmed_match` before anything is snapshotted."""
    reference = make_reference(member)
    page = playable(make_page("candidate"))
    make_hosting_record(page.site.domain)
    match(reference, page, band="high", status="proposed", copy_extent="full_copy")
    rights_ready(organisation, reference.sha256, member, staff)

    with pytest.raises(services.NoticeError) as error:
        services.create_draft(
            organisation,
            reference,
            [page.pk],
            recipient_choice(hostname=page.site.domain),
            "dsa_art16",
            "en",
            claimant_input(),
            work_title=reference.title,
            original_public_id="orig_deadbeefdeadbeefdeadbeefdeadbeef",
            actor=services.Actor.customer(member),
            now=NOW,
        )

    assert error.value.code == "finding_not_eligible"
    # `no_high_confirmed_match` first: nobody reviewed this copy, so there is no date to snapshot
    # and no draft to snapshot it into. (The extent tick is refused for the same reason — an
    # unconfirmed row's extent is `unknown`.)
    assert error.value.detail[page.public_id][0] == "no_high_confirmed_match"
