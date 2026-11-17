"""The rights status machine, its log, competing claims, mandates and schedules (ADR 0034)."""

from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from accounts.models import Organisation
from marceline.rights import services
from marceline.rights.declarations import CURRENT_AUTHORITY_DECLARATION
from marceline.rights.models import (
    RightsEvent,
    RightsSchedule,
    RightsStatus,
    WorkRights,
    WorkRightsTransition,
)
from marceline.tests.app_helpers import add_member, make_organisation
from marceline.tests.rights_helpers import (
    declaration,
    declared,
    documents_complete,
    mandate,
    staff_user,
)

SHA = "a" * 64
S = RightsStatus


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "member@example.test")


@pytest.fixture
def staff(db: None) -> User:
    return staff_user()


def test_a_self_declaration_is_logged_with_its_text_and_version(
    organisation: Organisation, member: User
) -> None:
    work = declared(organisation, SHA, member, language="de")

    assert work.status == S.SELF_DECLARED
    assert work.declaration_language == "de"
    assert work.declaration_text.startswith("Ich erkläre")
    assert work.declared_by == member
    log = list(
        work.transitions.values_list("from_status", "to_status", "actor_kind", "actor_label")
    )
    assert log == [
        ("", S.UNVERIFIED, "customer", "member@example.test"),
        (S.UNVERIFIED, S.SELF_DECLARED, "customer", "member@example.test"),
    ]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"confirmed": False}, "declaration_not_confirmed"),
        ({"declaration_version": "work-1999-01-01"}, "declaration_version_outdated"),
        ({"capacity": "authorised_agent"}, "underlying_owner_required"),
        ({"asserted_right": "other"}, "asserted_right_detail_required"),
        ({"language": "fr"}, "unknown_language"),
    ],
)
def test_a_declaration_that_is_incomplete_is_refused(
    organisation: Organisation, member: User, overrides: dict[str, object], code: str
) -> None:
    with pytest.raises(services.RightsError) as error:
        declared(organisation, SHA, member, **overrides)

    assert error.value.code == code
    assert not WorkRights.objects.exists()


def test_customers_cannot_go_beyond_self_declared(organisation: Organisation, member: User) -> None:
    work = declared(organisation, SHA, member)
    customer = services.Actor.customer(member)

    for target in (
        S.DOCUMENTS_RECEIVED,
        S.DOCUMENTS_COMPLETE,
        S.AUTHORITY_DECLARED,
        S.DISPUTED,
        S.REVOKED,
        S.SUPERSEDED,
    ):
        with pytest.raises(services.RightsError, match="not allowed"):
            services.transition(work, target, customer)

    assert services.allowed_targets(S.SELF_DECLARED, "customer") == [S.UNVERIFIED, S.SELF_DECLARED]


def test_documents_complete_needs_documents_received_first(
    organisation: Organisation, staff: User
) -> None:
    actor = services.Actor.staff(staff)
    work = services.open_record(organisation, SHA, actor)

    with pytest.raises(services.RightsError):
        services.transition(work, S.DOCUMENTS_COMPLETE, actor, reason="looks fine")

    work = documents_complete(organisation, SHA, staff)
    assert work.status == S.DOCUMENTS_COMPLETE
    assert [row.to_status for row in work.transitions.all()] == [
        S.UNVERIFIED,
        S.DOCUMENTS_RECEIVED,
        S.DOCUMENTS_COMPLETE,
    ]


def test_staff_can_never_declare_a_customer_authority(
    organisation: Organisation, member: User, staff: User
) -> None:
    """The affirmative statement about the documents is the customer's alone: there is no staff
    route to it, not even the generic `transition` the admin actions use."""
    work = documents_complete(organisation, SHA, staff)
    actor = services.Actor.staff(staff)

    with pytest.raises(services.RightsError, match="not allowed"):
        services.transition(
            work, S.AUTHORITY_DECLARED, actor, reason="the mandate clearly covers it"
        )
    with pytest.raises(services.RightsError, match="not allowed"):
        services.transition(work, S.AUTHORITY_DECLARED, services.SYSTEM, code="inferred")

    assert services.allowed_targets(S.DOCUMENTS_COMPLETE, "customer") == [S.AUTHORITY_DECLARED]


def test_staff_changes_need_a_reason(organisation: Organisation, staff: User) -> None:
    actor = services.Actor.staff(staff)
    work = services.open_record(organisation, SHA, actor)

    with pytest.raises(services.RightsError) as error:
        services.transition(work, S.DOCUMENTS_RECEIVED, actor, reason="  ")

    assert error.value.code == "reason_required"


def test_a_customer_cannot_redeclare_a_record_staff_are_checking(
    organisation: Organisation, member: User, staff: User
) -> None:
    work = declared(organisation, SHA, member)
    services.transition(work, S.DOCUMENTS_RECEIVED, services.Actor.staff(staff), reason="got it")

    with pytest.raises(services.RightsError) as error:
        declared(organisation, SHA, member)

    assert error.value.code == "declaration_not_open"


def test_the_log_is_append_only_in_the_database(organisation: Organisation, member: User) -> None:
    work = declared(organisation, SHA, member)
    row = work.transitions.last()
    assert row is not None

    with pytest.raises(IntegrityError), transaction.atomic():
        WorkRightsTransition.objects.filter(pk=row.pk).update(reason="rewritten")
    event = RightsEvent.objects.create(
        organisation=organisation, kind="token_issued", actor_kind="system"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        RightsEvent.objects.filter(pk=event.pk).update(actor_label="someone else")


def test_one_current_record_per_organisation_and_work(organisation: Organisation) -> None:
    WorkRights.objects.create(organisation=organisation, sha256=SHA)

    with pytest.raises(IntegrityError), transaction.atomic():
        WorkRights.objects.create(organisation=organisation, sha256=SHA)


def test_supersede_closes_the_record_and_opens_a_fresh_one(
    organisation: Organisation, staff: User
) -> None:
    old = documents_complete(organisation, SHA, staff)

    new = services.supersede(old, reason="rights transferred", actor=services.Actor.staff(staff))

    old.refresh_from_db()
    assert old.status == S.SUPERSEDED
    assert new.pk != old.pk and new.status == S.UNVERIFIED
    assert services.current_work(organisation, SHA) == new


def test_a_second_organisation_claiming_the_file_disputes_both(
    organisation: Organisation, member: User, staff: User
) -> None:
    first = documents_complete(organisation, SHA, staff)
    other = make_organisation("Performer")

    competing = services.record_competing_claim(
        other, SHA, reason="performer wrote in", actor=services.Actor.staff(staff)
    )

    first.refresh_from_db()
    assert first.status == S.DISPUTED
    assert competing.status == S.DISPUTED
    assert first.transitions.last().code == "competing_claim"  # type: ignore[union-attr]
    assert first.transitions.last().actor_kind == "system"  # type: ignore[union-attr]


def test_a_customer_declaration_on_a_claimed_file_disputes_both(
    organisation: Organisation, member: User
) -> None:
    other = make_organisation("Other Studio")
    other_member = add_member(other, "other@example.test")
    first = declared(organisation, SHA, member)

    second = declared(other, SHA, other_member)

    first.refresh_from_db()
    assert (first.status, second.status) == (S.DISPUTED, S.DISPUTED)


def test_a_closed_record_is_no_competing_claim(
    organisation: Organisation, member: User, staff: User
) -> None:
    other = make_organisation("Former Owner")
    old = documents_complete(other, SHA, staff)
    services.transition(old, S.REVOKED, services.Actor.staff(staff), reason="rights returned")

    work = declared(organisation, SHA, member)

    assert work.status == S.SELF_DECLARED


def test_a_dispute_is_resolved_only_by_staff(organisation: Organisation, staff: User) -> None:
    actor = services.Actor.staff(staff)
    work = documents_complete(organisation, SHA, staff)
    work = services.transition(work, S.DISPUTED, actor, reason="co-performer objects")

    assert services.allowed_targets(S.DISPUTED, "staff") == [
        S.UNVERIFIED,
        S.DOCUMENTS_RECEIVED,
        S.REVOKED,
        S.EXPIRED,
        S.SUPERSEDED,
    ]
    work = services.transition(work, S.DOCUMENTS_RECEIVED, actor, reason="agreement settles it")
    assert work.status == S.DOCUMENTS_RECEIVED


def test_revoking_a_mandate_revokes_its_works(organisation: Organisation, staff: User) -> None:
    agency_mandate = mandate(organisation)
    work = documents_complete(organisation, SHA, staff)
    WorkRights.objects.filter(pk=work.pk).update(
        capacity="authorised_agent", mandate=agency_mandate
    )
    closed = documents_complete(organisation, "b" * 64, staff)
    services.transition(closed, S.SUPERSEDED, services.Actor.staff(staff), reason="replaced")
    WorkRights.objects.filter(pk=closed.pk).update(mandate=agency_mandate)

    moved = services.revoke_mandate(
        agency_mandate, reason="principal ended the agency", actor=services.Actor.staff(staff)
    )

    work.refresh_from_db()
    agency_mandate.refresh_from_db()
    assert [row.pk for row in moved] == [work.pk]
    assert work.status == S.REVOKED
    assert work.transitions.last().code == "mandate_revoked"  # type: ignore[union-attr]
    assert agency_mandate.revoked_at is not None
    assert RightsEvent.objects.filter(kind="mandate_revoked", mandate=agency_mandate).count() == 1
    with pytest.raises(services.RightsError):
        services.revoke_mandate(agency_mandate, reason="again", actor=services.Actor.staff(staff))


def test_expired_mandates_and_schedules_expire_their_works(
    organisation: Organisation, staff: User
) -> None:
    today = date(2026, 9, 17)
    ended = mandate(organisation, valid_until=today - timedelta(days=1))
    running = mandate(organisation, valid_until=today)
    schedule = RightsSchedule.objects.create(
        organisation=organisation, name="Catalogue 2025", valid_until=date(2026, 6, 30)
    )
    under_ended = documents_complete(organisation, "1" * 64, staff)
    under_running = documents_complete(organisation, "2" * 64, staff)
    under_schedule = documents_complete(organisation, "3" * 64, staff)
    WorkRights.objects.filter(pk=under_ended.pk).update(mandate=ended)
    WorkRights.objects.filter(pk=under_running.pk).update(mandate=running)
    WorkRights.objects.filter(pk=under_schedule.pk).update(schedule=schedule)

    preview = services.expire_due(today, dry_run=True)
    assert {row.pk for row in preview.works} == {under_ended.pk, under_schedule.pk}
    assert WorkRights.objects.filter(status=S.EXPIRED).count() == 0

    result = services.expire_due(today)

    statuses = dict(WorkRights.objects.values_list("pk", "status"))
    assert statuses[under_ended.pk] == S.EXPIRED
    assert statuses[under_schedule.pk] == S.EXPIRED
    assert statuses[under_running.pk] == S.DOCUMENTS_COMPLETE
    assert [row.pk for row in result.mandates] == [ended.pk]
    assert services.expire_due(today).works == []  # idempotent


def test_verifying_a_schedule_verifies_every_work_it_covers(
    organisation: Organisation, member: User, staff: User
) -> None:
    schedule = RightsSchedule.objects.create(organisation=organisation, name="Catalogue")
    works = [declared(organisation, digit * 64, member) for digit in "456"]
    WorkRights.objects.filter(pk__in=[work.pk for work in works]).update(schedule=schedule)

    moved = services.verify_schedule(
        schedule, reason="licence schedule 3 lists all", actor=services.Actor.staff(staff)
    )

    assert len(moved) == 3
    assert set(WorkRights.objects.values_list("status", flat=True)) == {S.DOCUMENTS_COMPLETE}
    codes = WorkRightsTransition.objects.filter(to_status=S.DOCUMENTS_COMPLETE)
    assert {row.code for row in codes} == {"schedule_formal"}


def test_claimant_type_locks_once_documents_were_checked(
    organisation: Organisation, member: User, staff: User
) -> None:
    customer = services.Actor.customer(member)
    services.set_claimant_type(organisation, "self_producing_creator", customer)
    services.set_claimant_type(organisation, "studio_producer", customer)
    documents_complete(organisation, SHA, staff)

    with pytest.raises(services.RightsError) as error:
        services.set_claimant_type(organisation, "agency", customer)

    assert error.value.code == "claimant_type_locked"
    events = RightsEvent.objects.filter(kind="claimant_type_set").values_list("detail", flat=True)
    assert list(events) == [
        {"from": "", "to": "self_producing_creator"},
        {"from": "self_producing_creator", "to": "studio_producer"},
    ]


def test_identity_verification_is_staff_only_and_needs_evidence(
    organisation: Organisation, member: User, staff: User
) -> None:
    with pytest.raises(services.RightsError):
        services.verify_identity(
            organisation,
            method="commercial_register",
            evidence_note="HRB 1",
            actor=services.Actor.customer(member),
        )
    with pytest.raises(services.RightsError) as error:
        services.verify_identity(
            organisation,
            method="commercial_register",
            evidence_note="",
            actor=services.Actor.staff(staff),
        )
    assert error.value.code == "evidence_note_required"

    claimant = services.verify_identity(
        organisation,
        method="commercial_register",
        evidence_note="HRB 12345, Amtsgericht München",
        actor=services.Actor.staff(staff),
    )

    assert claimant.identity_status == "verified"
    assert claimant.identity_verified_by == staff


def test_partners_clear_a_none_affirmation(organisation: Organisation, member: User) -> None:
    customer = services.Actor.customer(member)
    claimant = services.affirm_no_partners(
        organisation,
        version="partners-none-2026-09-17",
        language="en",
        confirmed=True,
        actor=customer,
    )
    assert claimant.partners_none_affirmed_at is not None

    services.add_partner(
        organisation,
        services.PartnerInput(kind="domain", name="Shop", domain="https://www.Shop.example/"),
        customer,
    )

    claimant.refresh_from_db()
    assert claimant.partners_none_affirmed_at is None
    with pytest.raises(services.RightsError) as error:
        services.affirm_no_partners(
            organisation,
            version="partners-none-2026-09-17",
            language="en",
            confirmed=True,
            actor=customer,
        )
    assert error.value.code == "partners_exist"


def test_declaration_input_defaults_are_a_complete_owner_declaration() -> None:
    assert declaration().capacity == "owner"


def _withdrawal(**overrides: object) -> services.WithdrawalInput:
    values: dict[str, object] = {
        "reason": "rights_transferred",
        "declaration_version": "withdraw-2026-09-17",
        "language": "en",
        "confirmed": True,
    }
    values.update(overrides)
    return services.WithdrawalInput(**values)  # type: ignore[arg-type]


def test_a_customer_withdraws_their_own_self_declaration(
    organisation: Organisation, member: User
) -> None:
    declared(organisation, SHA, member)

    work = services.withdraw_declaration(
        organisation, SHA, _withdrawal(note="sold the catalogue"), services.Actor.customer(member)
    )

    assert work.status == S.UNVERIFIED
    assert (work.declared_at, work.declaration_text, work.declaration_version) == (None, "", "")
    last = work.transitions.last()
    assert last is not None
    assert (last.from_status, last.to_status, last.actor_kind, last.code) == (
        S.SELF_DECLARED,
        S.UNVERIFIED,
        "customer",
        "withdrawn",
    )
    assert (
        last.reason == "withdrawal withdraw-2026-09-17 (en): rights_transferred: sold the catalogue"
    )
    # It can be declared again afterwards.
    assert declared(organisation, SHA, member).status == S.SELF_DECLARED


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"confirmed": False}, "declaration_not_confirmed"),
        ({"declaration_version": "withdraw-2000-01-01"}, "declaration_version_outdated"),
        ({"reason": "bored"}, "unknown_withdrawal_reason"),
        ({"language": "fr"}, "unknown_language"),
    ],
)
def test_an_incomplete_withdrawal_is_refused(
    organisation: Organisation, member: User, overrides: dict[str, object], code: str
) -> None:
    declared(organisation, SHA, member)

    with pytest.raises(services.RightsError) as error:
        services.withdraw_declaration(
            organisation, SHA, _withdrawal(**overrides), services.Actor.customer(member)
        )

    assert error.value.code == code
    assert services.current_work(organisation, SHA).status == S.SELF_DECLARED  # type: ignore[union-attr]


@pytest.mark.parametrize("status", [S.DOCUMENTS_RECEIVED, S.DOCUMENTS_COMPLETE])
def test_once_staff_checked_documents_only_staff_change_it(
    organisation: Organisation, member: User, staff: User, status: str
) -> None:
    work = declared(organisation, SHA, member)
    actor = services.Actor.staff(staff)
    work = services.transition(work, S.DOCUMENTS_RECEIVED, actor, reason="contract")
    if status == S.DOCUMENTS_COMPLETE:
        services.transition(work, S.DOCUMENTS_COMPLETE, actor, reason="all fields present")

    with pytest.raises(services.RightsError) as error:
        services.withdraw_declaration(
            organisation, SHA, _withdrawal(), services.Actor.customer(member)
        )

    assert error.value.code == "declaration_not_withdrawable"
    with pytest.raises(services.RightsError):
        services.withdraw_declaration(
            organisation, "f" * 64, _withdrawal(), services.Actor.customer(member)
        )


# --- the customer's authority declaration (ADR 0034 amendment 2026-09-18) -------------------------


def _authority(**overrides: object) -> services.AuthorityInput:
    values: dict[str, object] = {
        "declaration_version": "authority-2026-09-18",
        "language": "en",
        "confirmed": True,
    }
    values.update(overrides)
    return services.AuthorityInput(**values)  # type: ignore[arg-type]


def test_a_customer_declares_authority_over_complete_documents(
    organisation: Organisation, member: User, staff: User
) -> None:
    documents_complete(organisation, SHA, staff)

    work = services.declare_authority(
        organisation, SHA, _authority(language="de"), services.Actor.customer(member)
    )

    assert work.status == S.AUTHORITY_DECLARED
    assert work.authority_declaration_version == "authority-2026-09-18"
    assert work.authority_declaration_language == "de"
    # The exact text shown is what is stored, like every other declaration.
    assert work.authority_declaration_text == CURRENT_AUTHORITY_DECLARATION.text("de")
    assert work.authority_declared_by == member
    row = work.transitions.order_by("pk").last()
    assert row is not None and row.code == "authority_declared" and row.actor_kind == "customer"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"confirmed": False}, "declaration_not_confirmed"),
        ({"declaration_version": "authority-2019-01-01"}, "declaration_version_outdated"),
        ({"language": "fr"}, "unknown_language"),
    ],
)
def test_an_incomplete_authority_declaration_is_refused(
    organisation: Organisation, member: User, staff: User, overrides: dict[str, object], code: str
) -> None:
    documents_complete(organisation, SHA, staff)

    with pytest.raises(services.RightsError) as error:
        services.declare_authority(
            organisation, SHA, _authority(**overrides), services.Actor.customer(member)
        )

    assert error.value.code == code
    assert services.current_work(organisation, SHA).status == S.DOCUMENTS_COMPLETE  # type: ignore[union-attr]


@pytest.mark.parametrize("status", [S.UNVERIFIED, S.SELF_DECLARED, S.DOCUMENTS_RECEIVED])
def test_authority_cannot_be_declared_before_the_documents_are_complete(
    organisation: Organisation, member: User, staff: User, status: str
) -> None:
    actor = services.Actor.staff(staff)
    work = services.open_record(organisation, SHA, actor)
    if status == S.SELF_DECLARED:
        declared(organisation, SHA, member)
    if status == S.DOCUMENTS_RECEIVED:
        services.transition(work, S.DOCUMENTS_RECEIVED, actor, reason="excerpt received")

    with pytest.raises(services.RightsError) as error:
        services.declare_authority(organisation, SHA, _authority(), services.Actor.customer(member))

    assert error.value.code == "authority_not_declarable"


def test_the_service_itself_refuses_a_staff_authority_declaration(
    organisation: Organisation, staff: User
) -> None:
    """Not only the status machine: the entry point a staff session could reach writes nothing."""
    documents_complete(organisation, SHA, staff)

    with pytest.raises(services.RightsError, match="not allowed"):
        services.declare_authority(organisation, SHA, _authority(), services.Actor.staff(staff))

    work = services.current_work(organisation, SHA)
    assert work is not None
    assert work.status == S.DOCUMENTS_COMPLETE
    assert work.authority_declaration_version == "", "rolled back with the refused transition"
    assert work.authority_declared_at is None


def test_a_customer_withdraws_their_authority_declaration(
    organisation: Organisation, member: User, staff: User
) -> None:
    documents_complete(organisation, SHA, staff)
    services.declare_authority(organisation, SHA, _authority(), services.Actor.customer(member))

    work = services.withdraw_authority(
        organisation,
        SHA,
        _withdrawal(declaration_version="authority-withdraw-2026-09-18", reason="authority_ended"),
        services.Actor.customer(member),
    )

    # The documents keep their formal check; only the customer's statement about them goes.
    assert work.status == S.DOCUMENTS_COMPLETE
    assert work.authority_declaration_version == ""
    assert work.authority_declaration_text == ""
    assert work.authority_declared_at is None
    row = work.transitions.order_by("pk").last()
    assert row is not None and row.code == "authority_withdrawn"
    assert "authority_ended" in row.reason


def test_reopening_a_record_clears_the_authority_declaration(
    organisation: Organisation, member: User, staff: User
) -> None:
    """New documents, new statement: a staff reopen never leaves a stale affirmation standing."""
    documents_complete(organisation, SHA, staff)
    services.declare_authority(organisation, SHA, _authority(), services.Actor.customer(member))
    work = services.current_work(organisation, SHA)
    assert work is not None

    services.transition(
        work, S.DOCUMENTS_RECEIVED, services.Actor.staff(staff), reason="a newer excerpt arrived"
    )

    work.refresh_from_db()
    assert work.authority_declaration_version == ""
    assert work.authority_declared_by is None
    # What was declared, and when, stays in the append-only log.
    assert [row.code for row in work.transitions.order_by("pk")][-2:] == [
        "authority_declared",
        "",
    ]
