"""The Add original rights tick (#29) is a self-declaration in the rights model (ADR 0034)."""

import pytest
from django.contrib.auth.models import User

from accounts.models import Organisation
from marceline.app.rights import CURRENT_VERSION, TEXTS
from marceline.models import RightsConfirmation
from marceline.rights import services
from marceline.rights.gate import Work, notice_gate_blockers
from marceline.rights.models import RightsStatus, WorkRights
from marceline.tests.app_helpers import add_member, client_for, make_organisation, make_reference
from marceline.tests.rights_helpers import declared, staff_user
from marceline.tests.test_app_fingerprints import body, post
from matching.models import Reference

pytestmark = pytest.mark.django_db

S = RightsStatus


@pytest.fixture
def studio() -> Organisation:
    return make_organisation("Novak Studio")


@pytest.fixture
def member(studio: Organisation) -> User:
    return add_member(studio, "member@studio.test")


def test_an_upload_self_declares_the_work_and_links_the_confirmation(
    studio: Organisation, member: User
) -> None:
    response = post(client_for(member), body(language="de"))

    assert response.status_code == 201, response.content
    reference = Reference.objects.get()
    work = WorkRights.objects.get()
    confirmation = RightsConfirmation.objects.get()
    assert (work.organisation, work.sha256, work.status) == (
        studio,
        reference.sha256,
        S.SELF_DECLARED,
    )
    assert confirmation.work_rights == work
    assert work.declaration_version == f"upload-{CURRENT_VERSION}"
    assert work.declaration_language == "de"
    assert work.declaration_text == TEXTS[CURRENT_VERSION]["de"]
    assert work.declared_by == member
    last = work.transitions.last()
    assert last is not None
    assert (last.to_status, last.actor_kind, last.code) == (
        S.SELF_DECLARED,
        "customer",
        "declared_at_upload",
    )
    # The tick names no capacity or right: the gate asks the customer to complete it.
    assert "declaration_incomplete" in notice_gate_blockers(Work(studio, reference.sha256))


def test_a_refused_upload_declares_nothing(member: User) -> None:
    response = post(client_for(member), body(rights_confirmed=False))

    assert response.status_code == 400
    assert not WorkRights.objects.exists()


def test_the_tick_never_downgrades_a_record_staff_work_on(
    studio: Organisation, member: User
) -> None:
    reference = make_reference(member)
    work = declared(studio, reference.sha256, member)
    work = services.transition(
        work, S.DOCUMENTS_RECEIVED, services.Actor.staff(staff_user()), reason="licence excerpt"
    )

    again = services.declare_at_upload(
        studio,
        reference.sha256,
        text_version=CURRENT_VERSION,
        language="en",
        text=TEXTS[CURRENT_VERSION]["en"],
        actor=services.Actor.customer(member),
    )

    assert again.pk == work.pk
    assert again.status == S.DOCUMENTS_RECEIVED
    assert again.capacity == "owner"  # the earlier, complete declaration is kept
    assert "declaration_incomplete" not in notice_gate_blockers(Work(studio, reference.sha256))


def test_the_tick_redeclares_a_self_declared_record(studio: Organisation, member: User) -> None:
    reference = make_reference(member)
    declared(studio, reference.sha256, member)

    work = services.declare_at_upload(
        studio,
        reference.sha256,
        text_version=CURRENT_VERSION,
        language="en",
        text=TEXTS[CURRENT_VERSION]["en"],
        actor=services.Actor.customer(member),
    )

    assert work.status == S.SELF_DECLARED
    assert work.capacity == "owner"
    assert [row.code for row in work.transitions.all()] == [
        "opened",
        "declared",
        "declared_at_upload",
    ]
