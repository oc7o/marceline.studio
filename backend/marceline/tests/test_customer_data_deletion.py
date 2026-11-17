"""Customer data deletion, both scopes (ADR 0045, issue #85).

One test per guarantee the ADR makes, because every one of them is a promise to a customer or a
refusal the database has to enforce: each scope deletes what it says, a file another organisation
owns is kept, an invoice is kept, a legal hold is kept, a safety incident is untouched, a dry run
writes nothing, a second run changes nothing, the scheduler skips the job while the flag is off,
and an owner switch marks the previous owner's data and nothing of the new owner's.

Two of them are about a table this ADR does not own: a recorded "no" (ADR 0047,
`docs/do-not-contact.md`) has to survive the deletion of the customer it was linked to, because a
prospect who became a customer and left is still someone who said no.
"""

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Membership, Organisation
from billing.models import Invoice
from catalog.do_not_contact import EMAIL
from catalog.models import Creator
from catalog.services import contact_check, record_no_contact
from delivery.models import PurgeOutbox
from index.models import Page, SafetyIncident, SafetyIncidentPage
from marceline.app.ownership import Via, owned_references, reassign, with_twins
from marceline.deletion import handover, retention, services
from marceline.deletion.export import export_document
from marceline.deletion.models import DataDeletionRecord, OrganisationEnding, OriginalRemoval
from marceline.deletion.retention import (
    KEPT_BILLING,
    KEPT_LEGAL_HOLD,
    KEPT_SHARED_FILE,
    KEPT_STAFF_ACCOUNT,
    apply_retention,
)
from marceline.models import OriginalOwnership, OriginalPublicId
from marceline.notices.models import NoticeDraft, NoticeEvent
from marceline.rights import account_control_services as verification
from marceline.rights import services as rights_services
from marceline.rights.models import AccountControlChallenge, ProofDocument, WorkRights
from marceline.scheduler import JOBS
from marceline.tests.app_helpers import add_member, make_organisation, make_reference, own
from marceline.tests.rights_helpers import declared, staff_user
from matching.models import CaseFinding, LeakCase, Reference, ReferenceFrameHash

pytestmark = pytest.mark.django_db

PAST = timedelta(days=31)


def ended(organisation: Organisation) -> OrganisationEnding:
    """An organisation whose pilot ended long enough ago to be due."""
    row, _ = services.end_pilot(organisation, ended_at=timezone.now() - PAST)
    return row


def removed(organisation: Organisation, sha256: str) -> OriginalRemoval:
    row, _ = services.remove_original(
        organisation, sha256, removed_at=timezone.now() - PAST, reason=OriginalRemoval.Reason.STAFF
    )
    return row


def draft_for(organisation: Organisation, sha256: str) -> NoticeDraft:
    draft = NoticeDraft.objects.create(
        organisation=organisation,
        language="en",
        rendered_variant="dsa_art16",
        is_sandbox=False,
        recipient_hostname="host.example.test",
        # A real `catalog.ComplaintChannel.Kind`, as the field's help text says: the customer
        # schema refuses anything else, and since ADR 0046 the notices list is one of the
        # documents this export renders, so an invented value fails there.
        channel_kind="abuse_email",
        sha256=sha256,
        original_public_id="orig_" + "0" * 32,
        work_title="An original",
    )
    NoticeEvent.objects.create(draft=draft, kind="created", actor_kind="customer")
    return draft


def frames_for(reference: Reference, count: int = 2) -> None:
    ReferenceFrameHash.objects.bulk_create(
        ReferenceFrameHash(
            reference=reference,
            frame_index=index,
            timestamp_s=float(index),
            pdq=bytes([index]) * 32,
            quality=100,
        )
        for index in range(count)
    )


def test_organisation_scope_deletes_what_the_customer_registered(
    make_page: Callable[[str], Page],
) -> None:
    organisation = make_organisation("Leaving Customer")
    user = add_member(organisation, "leaving")
    reference = make_reference(user)
    frames_for(reference)
    case = LeakCase.objects.create(reference=reference, opened_at=timezone.now())
    CaseFinding.objects.create(case=case, page=make_page("a"), discovered_at=timezone.now())
    draft_for(organisation, reference.sha256)
    declared(organisation, reference.sha256, user)
    ended(organisation)

    report = apply_retention(dry_run=False)

    [scope] = report.organisations
    assert scope.complete
    assert not Organisation.objects.filter(pk=organisation.pk).exists()
    assert not User.objects.filter(pk=user.pk).exists()
    assert not Reference.objects.filter(pk=reference.pk).exists()
    assert not ReferenceFrameHash.objects.exists()
    assert not LeakCase.objects.exists()
    assert not CaseFinding.objects.exists()
    assert not NoticeDraft.objects.exists()
    assert not WorkRights.objects.exists()
    assert not OriginalOwnership.objects.exists()
    assert not OriginalPublicId.objects.exists()
    # The audit record survives, with no name and no URL in it.
    [record] = DataDeletionRecord.objects.all()
    assert record.scope == DataDeletionRecord.Scope.ORGANISATION
    assert record.organisation_pk == organisation.pk
    assert record.counts["originals"] == 1
    assert record.window_days == 30


def test_organisation_scope_keeps_the_pages_it_matched(make_page: Callable[[str], Page]) -> None:
    """Pages are ours, not the customer's: a customer leaving does not unmake the index."""
    organisation = make_organisation("Leaving Customer")
    user = add_member(organisation, "leaving")
    reference = make_reference(user)
    page = make_page("still-here")
    case = LeakCase.objects.create(reference=reference, opened_at=timezone.now())
    CaseFinding.objects.create(case=case, page=page, discovered_at=timezone.now())
    ended(organisation)

    apply_retention(dry_run=False)

    assert Page.objects.filter(pk=page.pk).exists()


def test_original_scope_deletes_one_file_only() -> None:
    organisation = make_organisation("Customer")
    user = add_member(organisation, "customer")
    going = make_reference(user, title="Going")
    staying = make_reference(user, title="Staying")
    draft_for(organisation, going.sha256)
    draft_for(organisation, staying.sha256)
    declared(organisation, going.sha256, user)
    removed(organisation, going.sha256)

    report = apply_retention(dry_run=False)

    [scope] = report.originals
    assert scope.sha256 == going.sha256
    assert not Reference.objects.filter(pk=going.pk).exists()
    assert Reference.objects.filter(pk=staying.pk).exists()
    assert NoticeDraft.objects.filter(sha256=staying.sha256).exists()
    assert not NoticeDraft.objects.filter(sha256=going.sha256).exists()
    assert not WorkRights.objects.filter(sha256=going.sha256).exists()
    assert Organisation.objects.filter(pk=organisation.pk).exists()
    assert User.objects.filter(pk=user.pk).exists()


def test_a_file_another_organisation_owns_is_never_deleted() -> None:
    """The reference set is filtered by ownership at deletion time, both scopes."""
    leaving = make_organisation("Leaving")
    other = make_organisation("Other")
    user = add_member(leaving, "leaving")
    shared = make_reference(user)
    twin = Reference.objects.create(
        creator=shared.creator,
        title=shared.title,
        duration_s=shared.duration_s,
        sha256=shared.sha256,
        frame_fps=2,
    )
    own(twin, other)
    ended(leaving)

    report = apply_retention(dry_run=False)

    [scope] = report.organisations
    assert KEPT_SHARED_FILE in scope.kept
    assert Reference.objects.filter(pk=twin.pk).exists()
    assert Reference.objects.filter(pk=shared.pk).exists()
    assert Organisation.objects.filter(pk=other.pk).exists()


def test_an_invoice_keeps_the_organisation_row() -> None:
    organisation = make_organisation("Invoiced")
    user = add_member(organisation, "invoiced")
    reference = make_reference(user)
    invoice = Invoice.objects.create(organisation=organisation)
    ending = ended(organisation)

    report = apply_retention(dry_run=False)

    [scope] = report.organisations
    assert KEPT_BILLING in scope.kept
    assert not scope.complete
    assert Invoice.objects.filter(pk=invoice.pk).exists()
    assert Organisation.objects.filter(pk=organisation.pk).exists()
    # Everything that is not billing still went, and the mark stays pending so the job retries.
    assert not Reference.objects.filter(pk=reference.pk).exists()
    ending.refresh_from_db()
    assert ending.deleted_at is None


def test_a_claimed_platform_account_goes_with_the_rights_block(settings: Settings) -> None:
    """ADR 0055: a claimed handle is the customer's own data, so it leaves with everything else.

    Named in `_delete_rights` rather than left to the organisation row's CASCADE, because one legal
    hold keeps that row — and the handle must not be the last thing standing.
    """
    settings.ACCOUNT_CONTROL_VERIFICATION_ENABLED = True
    organisation = make_organisation("Leaving Creator")
    user = add_member(organisation, "leaving-creator")
    verification.claim_account(
        organisation,
        verification.ClaimInput(platform="fanhub", handle="leaving.creator"),
        rights_services.Actor.customer(user),
    )
    ended(organisation)

    plan = apply_retention(dry_run=True)
    report = apply_retention(dry_run=False)

    assert plan.organisations[0].counts["account_challenges"] == 1
    assert report.organisations[0].complete
    assert not AccountControlChallenge.objects.exists()


def test_the_export_carries_the_claimed_accounts_but_never_a_code(settings: Settings) -> None:
    """ADR 0045 + ADR 0055: a claimed handle is the customer's own data and travels with the rest.

    The `account_verification` section carries **no code field at all** — a settled claim has none
    and a live one is on the screen the customer is reading anyway. The `documents` section is a
    different thing and deliberately so: it is what the customer API answered, byte for byte, and
    their own live code is part of their own screen there.
    """
    settings.ACCOUNT_CONTROL_VERIFICATION_ENABLED = True
    organisation = make_organisation("Exporting Creator")
    user = add_member(organisation, "exporting-creator")
    challenge = verification.claim_account(
        organisation,
        verification.ClaimInput(platform="fanhub", handle="exporting.creator"),
        rights_services.Actor.customer(user),
    )

    document = export_document(organisation)

    [claimed] = document["account_verification"]
    assert claimed["handle"] == "exporting.creator"
    assert claimed["state"] == "pending"
    assert [event["kind"] for event in claimed["events"]] == ["shown"]
    assert "code" not in claimed
    assert challenge.code not in json.dumps(document["account_verification"])
    assert challenge.code not in json.dumps(document["notice_drafts"])


def test_a_legal_hold_keeps_the_rights_record() -> None:
    organisation = make_organisation("Held")
    user = add_member(organisation, "held")
    reference = make_reference(user)
    work = declared(organisation, reference.sha256, user)
    WorkRights.objects.filter(pk=work.pk).update(legal_hold=True)
    ended(organisation)

    report = apply_retention(dry_run=False)

    [scope] = report.organisations
    assert KEPT_LEGAL_HOLD in scope.kept
    assert WorkRights.objects.filter(pk=work.pk).exists()
    assert Organisation.objects.filter(pk=organisation.pk).exists()
    assert not Reference.objects.filter(pk=reference.pk).exists()


def test_a_held_rights_row_cannot_be_deleted_by_raw_sql() -> None:
    """The hold is refused by the database, not only by Python (ADR 0045, `marceline/0017`)."""
    organisation = make_organisation("Held")
    user = add_member(organisation, "held")
    reference = make_reference(user)
    work = declared(organisation, reference.sha256, user)
    WorkRights.objects.filter(pk=work.pk).update(legal_hold=True)

    with pytest.raises(IntegrityError), transaction.atomic():
        WorkRights.objects.filter(pk=work.pk).delete()

    assert WorkRights.objects.filter(pk=work.pk).exists()


def test_a_deletion_record_can_be_neither_changed_nor_deleted() -> None:
    record = DataDeletionRecord.objects.create(
        scope=DataDeletionRecord.Scope.ORGANISATION,
        organisation_pk=1,
        marked_at=timezone.now(),
        due_at=timezone.now(),
        window_days=30,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        DataDeletionRecord.objects.filter(pk=record.pk).update(window_days=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        DataDeletionRecord.objects.filter(pk=record.pk).delete()

    assert DataDeletionRecord.objects.filter(pk=record.pk).exists()


def test_a_safety_incident_is_untouched(make_page: Callable[[str], Page]) -> None:
    organisation = make_organisation("Leaving")
    user = add_member(organisation, "leaving")
    reference = make_reference(user)
    page = make_page("flagged")
    incident = SafetyIncident.objects.create(
        category="test",
        term="term",
        detected_at=timezone.now(),
        last_seen_at=timezone.now(),
    )
    link = SafetyIncidentPage.objects.create(
        incident=incident,
        page=page,
        url=page.url,
        evidence=SafetyIncidentPage.Evidence.MANUAL,
        first_seen_at=timezone.now(),
        last_seen_at=timezone.now(),
    )
    case = LeakCase.objects.create(reference=reference, opened_at=timezone.now())
    CaseFinding.objects.create(case=case, page=page, discovered_at=timezone.now())
    ended(organisation)

    apply_retention(dry_run=False)

    incident.refresh_from_db()
    link.refresh_from_db()
    assert link.page_id == page.pk
    assert Page.objects.filter(pk=page.pk).exists()


def test_a_dry_run_writes_nothing() -> None:
    organisation = make_organisation("Leaving")
    user = add_member(organisation, "leaving")
    reference = make_reference(user)
    draft_for(organisation, reference.sha256)
    ending = ended(organisation)

    report = apply_retention(dry_run=True)

    [scope] = report.organisations
    assert scope.counts["originals"] == 1
    assert Reference.objects.filter(pk=reference.pk).exists()
    assert NoticeDraft.objects.exists()
    assert Organisation.objects.filter(pk=organisation.pk).exists()
    assert not DataDeletionRecord.objects.exists()
    ending.refresh_from_db()
    assert ending.deleted_at is None


def test_a_second_run_changes_nothing() -> None:
    organisation = make_organisation("Leaving")
    user = add_member(organisation, "leaving")
    make_reference(user)
    ended(organisation)

    apply_retention(dry_run=False)
    records = DataDeletionRecord.objects.count()
    second = apply_retention(dry_run=False)

    assert second.due == 0
    assert DataDeletionRecord.objects.count() == records


def test_a_staff_account_keeps_its_login() -> None:
    organisation = make_organisation("Leaving")
    customer = add_member(organisation, "customer")
    reviewer = add_member(organisation, "reviewer", is_staff=True)
    ended(organisation)

    report = apply_retention(dry_run=False)

    [scope] = report.organisations
    assert KEPT_STAFF_ACCOUNT in scope.kept
    assert User.objects.filter(pk=reviewer.pk).exists()
    assert not User.objects.filter(pk=customer.pk).exists()
    assert not Membership.objects.filter(user=reviewer).exists()


def test_a_do_not_contact_record_outlives_the_customer_it_was_linked_to() -> None:
    """A prospect who became a customer and left is still someone who said no (ADR 0047).

    The record links a prospect `catalog.Creator` with `SET_NULL`, so the danger is a deletion pass
    reaching it sideways: through the creator, through the user who recorded it, or through the
    organisation. None of those may take the objection with them.
    """
    key = "test-key-not-a-real-one"
    organisation = make_organisation("Leaving")
    user = add_member(organisation, "leaving")
    make_reference(user)
    creator = Creator.objects.create(name="Invented Prospect")
    record_no_contact(
        kind=EMAIL,
        identifier="post@example.org",
        received_on=timezone.now().date(),
        channel="letter",
        recorded_by=user.get_username(),
        creator=creator,
        secret=key,
    )
    ended(organisation)

    apply_retention(dry_run=False)

    assert not Organisation.objects.filter(pk=organisation.pk).exists()
    assert not User.objects.filter(pk=user.pk).exists()
    assert Creator.objects.filter(pk=creator.pk).exists()
    assert contact_check(EMAIL, ["post@example.org"], key) == [True]


def test_the_deletion_pass_imports_nothing_from_catalog() -> None:
    """Belt and braces for the test above: the pass cannot delete what it never names.

    `catalog` holds the do-not-contact record, the creators, the sites and the safety terms — none
    of it a customer's data. If a future scope ever needs a catalog row, it needs its own decision
    and its own test, not an import that slips in.
    """
    package = Path(retention.__file__).parent
    for module in sorted(package.glob("*.py")):
        imports = [
            line
            for line in module.read_text().splitlines()
            if line.startswith(("import catalog", "from catalog"))
        ]
        assert imports == [], f"{module.name} imports {imports} — see ADR 0047"


def test_the_deletion_purges_the_production_cache(settings: Settings) -> None:
    settings.CACHE_PURGE_OUTBOX = True
    organisation = make_organisation("Leaving")
    add_member(organisation, "leaving")
    ended(organisation)

    apply_retention(dry_run=False)

    [purge] = PurgeOutbox.objects.all()
    assert purge.mode == PurgeOutbox.Mode.DELETE
    assert purge.organisation_ids == [organisation.pk]
    assert purge.reason == "customer_deletion"


def test_nothing_happens_without_a_mark() -> None:
    organisation = make_organisation("Happy Customer")
    user = add_member(organisation, "happy")
    reference = make_reference(user)

    report = apply_retention(dry_run=False)

    assert report.due == 0
    assert Reference.objects.filter(pk=reference.pk).exists()
    assert Organisation.objects.filter(pk=organisation.pk).exists()


def test_a_mark_younger_than_the_window_is_not_due(settings: Settings) -> None:
    organisation = make_organisation("Recent")
    user = add_member(organisation, "recent")
    reference = make_reference(user)
    services.end_pilot(organisation, ended_at=timezone.now() - timedelta(days=29))

    report = apply_retention(dry_run=False)

    assert report.due == 0
    assert report.pending_organisations == 1
    assert Reference.objects.filter(pk=reference.pk).exists()


def test_the_scheduler_job_is_off_and_says_why(settings: Settings) -> None:
    [job] = [entry for entry in JOBS if entry.name == "customer_data_retention"]
    # After page retention, and before only `research_retention` (ADR 0048), which judges the
    # catalogue names this pass frees.
    assert JOBS[-2] is job, "customer data deletion runs after page retention"
    assert JOBS[-1].name == "research_retention"
    assert job.guard is not None
    context = type("Context", (), {"now": timezone.now(), "last_succeeded_at": None})()
    assert job.guard(context) == "customer data deletion is off (CUSTOMER_DATA_DELETION_ENABLED)"
    settings.CUSTOMER_DATA_DELETION_ENABLED = True
    assert job.guard(context) is None


# --- the owner switch (ADR 0045, "on an owner switch") --------------------------------------


def test_an_owner_switch_marks_the_previous_owner_and_resets_the_case(
    make_page: Callable[[str], Page],
) -> None:
    previous = make_organisation("Previous")
    new = make_organisation("New")
    user = add_member(previous, "previous")
    add_member(new, "new")
    reference = make_reference(user)
    case = LeakCase.objects.create(reference=reference, opened_at=timezone.now(), reopen_count=2)
    CaseFinding.objects.create(case=case, page=make_page("p"), discovered_at=timezone.now())
    draft_for(previous, reference.sha256)

    reassign(with_twins([reference.pk]), new, via=Via.COMMAND)

    # The work moved; the previous owner's case history did not travel with it.
    assert owned_references(new).filter(pk=reference.pk).exists()
    assert not LeakCase.objects.exists()
    assert not CaseFinding.objects.exists()
    # The previous owner's remaining data is marked, not deleted yet.
    [mark] = OriginalRemoval.objects.all()
    assert mark.organisation_id == previous.pk
    assert mark.sha256 == reference.sha256
    assert mark.reason == OriginalRemoval.Reason.OWNER_CHANGED
    assert NoticeDraft.objects.filter(organisation=previous).exists()


def test_an_owner_switch_marks_nothing_of_the_new_owner() -> None:
    previous = make_organisation("Previous")
    new = make_organisation("New")
    user = add_member(previous, "previous")
    add_member(new, "new")
    reference = make_reference(user)

    reassign(with_twins([reference.pk]), new, via=Via.COMMAND)

    assert not OriginalRemoval.objects.filter(organisation=new).exists()
    assert not OrganisationEnding.objects.exists()


def test_unassigning_marks_the_previous_owner_too() -> None:
    previous = make_organisation("Previous")
    user = add_member(previous, "previous")
    reference = make_reference(user)

    reassign(with_twins([reference.pk]), None, via=Via.COMMAND)

    [mark] = OriginalRemoval.objects.all()
    assert mark.reason == OriginalRemoval.Reason.UNASSIGNED


def test_a_dry_run_switch_marks_nothing() -> None:
    previous = make_organisation("Previous")
    new = make_organisation("New")
    user = add_member(previous, "previous")
    reference = make_reference(user)
    case = LeakCase.objects.create(reference=reference, opened_at=timezone.now())

    changes = reassign(with_twins([reference.pk]), new, via=Via.COMMAND, dry_run=True)

    assert [change.changed for change in changes] == [True]
    assert not OriginalRemoval.objects.exists()
    assert LeakCase.objects.filter(pk=case.pk).exists()
    assert owned_references(previous).filter(pk=reference.pk).exists()


def test_the_switch_is_shown_before_it_happens(capsys: pytest.CaptureFixture[str]) -> None:
    previous = make_organisation("Previous")
    new = make_organisation("New")
    user = add_member(previous, "previous")
    reference = make_reference(user)
    LeakCase.objects.create(reference=reference, opened_at=timezone.now())

    call_command(
        "assign_originals", "--organisation", "New", "--reference", reference.pk, "--dry-run"
    )

    printed = capsys.readouterr().out
    assert f"organisation {previous.pk} loses file" in printed
    assert "would reset 1 case file(s)" in printed
    assert "would mark for removal (owner_changed)" in printed
    assert "dry run: nothing written" in printed
    assert not OriginalRemoval.objects.exists()
    assert LeakCase.objects.exists()
    assert not owned_references(new).exists()


def test_the_switch_report_is_right_on_a_real_run(capsys: pytest.CaptureFixture[str]) -> None:
    """After a real move the rows are gone, so the report has to be built before it (not after)."""
    previous = make_organisation("Previous")
    make_organisation("New")
    user = add_member(previous, "previous")
    reference = make_reference(user)
    LeakCase.objects.create(reference=reference, opened_at=timezone.now())

    call_command("assign_originals", "--organisation", "New", "--reference", reference.pk)

    printed = capsys.readouterr().out
    assert "reset 1 case file(s)" in printed
    assert "marked for removal (owner_changed)" in printed
    assert OriginalRemoval.objects.count() == 1


def test_the_previous_owner_loses_the_moved_file_after_the_window() -> None:
    """The mark an owner switch wrote deletes only the previous owner's rows, never the work."""
    previous = make_organisation("Previous")
    new = make_organisation("New")
    user = add_member(previous, "previous")
    add_member(new, "new")
    reference = make_reference(user)
    draft = draft_for(previous, reference.sha256)
    reassign(with_twins([reference.pk]), new, via=Via.COMMAND)
    OriginalRemoval.objects.update(removed_at=timezone.now() - PAST)

    report = apply_retention(dry_run=False)

    [scope] = report.originals
    assert KEPT_SHARED_FILE in scope.kept
    assert not NoticeDraft.objects.filter(pk=draft.pk).exists()
    assert Reference.objects.filter(pk=reference.pk).exists()
    assert owned_references(new).filter(pk=reference.pk).exists()


def test_a_second_move_keeps_the_first_clock() -> None:
    previous = make_organisation("Previous")
    middle = make_organisation("Middle")
    user = add_member(previous, "previous")
    reference = make_reference(user)
    reassign(with_twins([reference.pk]), middle, via=Via.COMMAND)
    first = OriginalRemoval.objects.get(organisation=previous)

    handover.apply(handover.plan([(previous.pk, previous.name, reference.sha256, False)]))

    first.refresh_from_db()
    assert OriginalRemoval.objects.filter(organisation=previous).count() == 1
    assert first.deleted_at is None


# --- marking and the export -----------------------------------------------------------------


def test_marking_and_lifting_write_only_the_mark() -> None:
    organisation = make_organisation("Customer")
    user = add_member(organisation, "customer")
    reference = make_reference(user)

    call_command("end_pilot", "--organisation", "Customer", "--ended-on", "2026-09-30")
    assert not OrganisationEnding.objects.exists()

    call_command("end_pilot", "--organisation", "Customer", "--ended-on", "2026-09-30", "--apply")
    assert OrganisationEnding.objects.count() == 1
    assert Reference.objects.filter(pk=reference.pk).exists()

    call_command("end_pilot", "--organisation", "Customer", "--lift", "--apply")
    assert not OrganisationEnding.objects.exists()


def test_remove_original_marks_one_file() -> None:
    organisation = make_organisation("Customer")
    user = add_member(organisation, "customer")
    reference = make_reference(user)

    call_command("remove_original", "--organisation", "Customer", "--reference", reference.pk)
    assert not OriginalRemoval.objects.exists()

    call_command(
        "remove_original", "--organisation", "Customer", "--reference", reference.pk, "--apply"
    )
    [mark] = OriginalRemoval.objects.all()
    assert mark.sha256 == reference.sha256
    assert mark.reason == OriginalRemoval.Reason.CUSTOMER_REQUEST
    assert Reference.objects.filter(pk=reference.pk).exists()


def test_the_export_carries_hashes_metadata_and_drafts() -> None:
    organisation = make_organisation("Customer")
    user = add_member(organisation, "customer")
    reference = make_reference(user)
    frames_for(reference, count=3)
    draft_for(organisation, reference.sha256)

    document = export_document(organisation)

    assert document["format"] == "marceline.customer-export"
    assert document["organisation"]["id"] == organisation.public_id
    [entry] = document["fingerprints"]["originals"]
    assert entry["sha256"] == reference.sha256
    assert len(entry["frames"]) == 3
    assert all(len(frame["pdq"]) == 64 for frame in entry["frames"])
    assert len(document["notice_drafts"]) == 1
    assert document["notice_drafts"][0]["events"][0]["kind"] == "created"
    # The customer's own documents, rendered by the very views they were shown.
    assert "originals" in document["documents"]


def test_the_export_command_writes_a_file_and_stamps_the_ending(tmp_path: Path) -> None:
    organisation = make_organisation("Customer")
    user = add_member(organisation, "customer")
    make_reference(user)
    ending = ended(organisation)
    out = tmp_path / "export.json"

    call_command("export_customer_data", "--organisation", "Customer", "--out", str(out))

    assert out.exists() and out.stat().st_size > 0
    ending.refresh_from_db()
    assert ending.exported_at is not None


def test_the_retention_command_defaults_to_a_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    organisation = make_organisation("Leaving")
    user = add_member(organisation, "leaving")
    reference = make_reference(user)
    ended(organisation)

    call_command("customer_data_retention")

    printed = capsys.readouterr().out
    assert "dry run, nothing written" in printed
    assert "would delete" in printed
    assert Reference.objects.filter(pk=reference.pk).exists()


def test_rights_proofs_and_their_files_go_with_the_organisation(
    settings: Settings, tmp_path: Path
) -> None:
    from marceline.rights import services as rights_services
    from marceline.tests.rights_helpers import PDF, configure_vault

    configure_vault(settings, tmp_path)
    organisation = make_organisation("Leaving")
    user = add_member(organisation, "leaving")
    reference = make_reference(user)
    work = declared(organisation, reference.sha256, user)
    proof = rights_services.upload_proof(
        organisation,
        PDF,
        filename="proof.pdf",
        kind="contract_excerpt",
        actor=rights_services.Actor.staff(staff_user()),
        work=work,
    )
    assert list(tmp_path.rglob(f"{proof.storage_name}*"))
    ended(organisation)

    apply_retention(dry_run=False)

    assert not ProofDocument.objects.filter(pk=proof.pk).exists()
    assert not list(tmp_path.rglob(f"{proof.storage_name}*"))
    assert WorkRights.objects.count() == 0
