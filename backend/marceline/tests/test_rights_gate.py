"""`notice_gate_blockers`: every rights reason a notice draft may not be prepared (ADR 0034)."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User

from accounts.models import Organisation
from marceline.rights import services
from marceline.rights.gate import (
    Blocker,
    GateFacts,
    MandateFacts,
    Work,
    gate_blockers,
    notice_gate_blockers,
)
from marceline.rights.models import RightsSchedule, WorkRights
from marceline.tests.app_helpers import add_member, make_organisation, make_reference
from marceline.tests.rights_helpers import (
    authority_declared,
    declared,
    documents_complete,
    mandate,
    staff_user,
)

TODAY = date(2026, 9, 17)

CLEAR = GateFacts(
    today=TODAY,
    owned=True,
    claimant_type="studio_producer",
    identity_status="verified",
    status="authority_declared",
    capacity="owner",
    asserted_right="copyright",
    partners_none_affirmed=True,
)


def test_an_owner_who_declared_authority_with_affirmed_partners_has_no_blockers() -> None:
    assert gate_blockers(CLEAR) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"owned": False}, [Blocker.WORK_NOT_OWNED]),
        ({"claimant_type": ""}, [Blocker.CLAIMANT_TYPE_MISSING]),
        ({"identity_status": "unverified"}, [Blocker.IDENTITY_NOT_VERIFIED]),
        ({"identity_status": "revoked"}, [Blocker.IDENTITY_NOT_VERIFIED]),
        ({"status": None}, [Blocker.RIGHTS_RECORD_MISSING]),
        ({"status": "unverified"}, [Blocker.DOCUMENTS_NOT_COMPLETE]),
        ({"status": "self_declared"}, [Blocker.DOCUMENTS_NOT_COMPLETE]),
        ({"status": "documents_received"}, [Blocker.DOCUMENTS_NOT_COMPLETE]),
        # Our formal check has passed; the customer's own statement about the documents has not
        # been made yet, and nothing else can stand in for it.
        ({"status": "documents_complete"}, [Blocker.AUTHORITY_NOT_DECLARED]),
        ({"status": "disputed"}, [Blocker.RIGHTS_DISPUTED]),
        ({"status": "revoked"}, [Blocker.RIGHTS_REVOKED]),
        ({"status": "expired"}, [Blocker.RIGHTS_EXPIRED]),
        ({"competing_claims": 1}, [Blocker.COMPETING_CLAIM]),
        ({"partners_none_affirmed": False}, [Blocker.PARTNERS_NOT_AFFIRMED]),
        ({"partners_none_affirmed": False, "partner_count": 2}, []),
        ({"schedule_valid_until": TODAY - timedelta(days=1)}, [Blocker.SCHEDULE_EXPIRED]),
        ({"schedule_valid_until": TODAY}, []),
        ({"asserted_right": ""}, [Blocker.DECLARATION_INCOMPLETE]),
        (
            {"status": "self_declared", "capacity": ""},
            [Blocker.DOCUMENTS_NOT_COMPLETE, Blocker.DECLARATION_INCOMPLETE],
        ),
        (
            {"status": "unverified", "capacity": "", "asserted_right": ""},
            [Blocker.DOCUMENTS_NOT_COMPLETE],
        ),
    ],
)
def test_each_missing_fact_is_one_blocker(
    change: dict[str, object], expected: list[Blocker]
) -> None:
    assert gate_blockers(replace(CLEAR, **change)) == expected  # type: ignore[arg-type]


VALID_MANDATE = MandateFacts(
    valid_from=date(2026, 1, 1), valid_until=None, revoked=False, authority_to_prepare_notices=True
)


@pytest.mark.parametrize(
    ("mandate_facts", "owner_name", "expected"),
    [
        (VALID_MANDATE, "Rights Holder GmbH", []),
        (None, "Rights Holder GmbH", [Blocker.MANDATE_MISSING]),
        (VALID_MANDATE, " ", [Blocker.UNDERLYING_OWNER_MISSING]),
        (replace(VALID_MANDATE, revoked=True), "X", [Blocker.MANDATE_REVOKED]),
        (
            replace(VALID_MANDATE, valid_from=TODAY + timedelta(days=1)),
            "X",
            [Blocker.MANDATE_NOT_YET_VALID],
        ),
        (
            replace(VALID_MANDATE, valid_until=TODAY - timedelta(days=1)),
            "X",
            [Blocker.MANDATE_EXPIRED],
        ),
        (replace(VALID_MANDATE, valid_until=TODAY), "X", []),
        (
            replace(VALID_MANDATE, authority_to_prepare_notices=False),
            "X",
            [Blocker.MANDATE_NOTICE_CLAUSE_NOT_RECORDED],
        ),
    ],
)
def test_an_agent_needs_a_current_mandate_with_notice_authority(
    mandate_facts: MandateFacts | None, owner_name: str, expected: list[Blocker]
) -> None:
    facts = replace(
        CLEAR, capacity="authorised_agent", underlying_owner_name=owner_name, mandate=mandate_facts
    )

    assert gate_blockers(facts) == expected


def test_blockers_come_in_a_fixed_order() -> None:
    facts = GateFacts(
        today=TODAY, owned=False, claimant_type="", identity_status="unverified", status=None
    )

    assert gate_blockers(facts) == [
        Blocker.WORK_NOT_OWNED,
        Blocker.CLAIMANT_TYPE_MISSING,
        Blocker.IDENTITY_NOT_VERIFIED,
        Blocker.RIGHTS_RECORD_MISSING,
        Blocker.PARTNERS_NOT_AFFIRMED,
    ]


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "member@example.test")


def test_from_the_database_a_new_customer_is_blocked_on_everything(
    organisation: Organisation, member: User
) -> None:
    reference = make_reference(member)

    assert notice_gate_blockers(Work(organisation, reference.sha256), today=TODAY) == [
        "claimant_type_missing",
        "identity_not_verified",
        "rights_record_missing",
        "partners_not_affirmed",
    ]


def test_from_the_database_the_full_path_clears_the_gate(
    organisation: Organisation, member: User
) -> None:
    staff = staff_user()
    reference = make_reference(member)
    customer = services.Actor.customer(member)
    work = Work(organisation, reference.sha256)
    services.set_claimant_type(organisation, "self_producing_creator", customer)
    services.affirm_no_partners(
        organisation,
        version="partners-none-2026-09-17",
        language="en",
        confirmed=True,
        actor=customer,
    )
    declared(organisation, reference.sha256, member)
    assert notice_gate_blockers(work, today=TODAY) == [
        "identity_not_verified",
        "documents_not_complete",
    ]

    services.verify_identity(
        organisation,
        method="account_control_token",
        evidence_note="token seen on the creator's sales profile",
        actor=services.Actor.staff(staff),
    )
    documents_complete(organisation, reference.sha256, staff)

    # Everything staff can do is done, and the gate still holds: the statement that those
    # documents give the claimed right is the customer's to make (ADR 0034 amendment 2026-09-18).
    assert notice_gate_blockers(work, today=TODAY) == ["authority_not_declared"]

    authority_declared(organisation, reference.sha256, member)

    assert notice_gate_blockers(work, today=TODAY) == []


def test_from_the_database_a_foreign_claim_and_a_moved_file_block(
    organisation: Organisation, member: User
) -> None:
    staff = staff_user()
    reference = make_reference(member)
    documents_complete(organisation, reference.sha256, staff)
    other = make_organisation("Performer")
    services.record_competing_claim(
        other, reference.sha256, reason="performer claims", actor=services.Actor.staff(staff)
    )

    assert {"rights_disputed", "competing_claim"} <= set(
        notice_gate_blockers(Work(organisation, reference.sha256), today=TODAY)
    )
    assert "work_not_owned" in notice_gate_blockers(Work(other, reference.sha256), today=TODAY)


def test_from_the_database_mandate_and_schedule_dates_are_read_live(
    organisation: Organisation, member: User
) -> None:
    staff = staff_user()
    reference = make_reference(member)
    work = documents_complete(organisation, reference.sha256, staff)
    schedule = RightsSchedule.objects.create(
        organisation=organisation, name="Catalogue", valid_until=TODAY - timedelta(days=3)
    )
    WorkRights.objects.filter(pk=work.pk).update(
        capacity="authorised_agent",
        underlying_owner_name="Rights Holder GmbH",
        mandate=mandate(organisation, valid_until=TODAY - timedelta(days=1)),
        schedule=schedule,
    )

    blockers = notice_gate_blockers(Work(organisation, reference.sha256), today=TODAY)

    assert "mandate_expired" in blockers
    assert "schedule_expired" in blockers
