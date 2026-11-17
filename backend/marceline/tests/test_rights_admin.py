"""Staff actions on rights in the admin: confirmation first, then a logged change (ADR 0034)."""

from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from marceline.rights import services
from marceline.rights.models import (
    AgencyMandate,
    ProofDocument,
    RightsClaimant,
    RightsEvent,
    RightsSchedule,
    WorkRights,
    WorkRightsTransition,
)
from marceline.tests.app_helpers import add_member, make_organisation
from marceline.tests.rights_helpers import PDF, configure_vault, declared, mandate

SHA = "d" * 64


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "member@example.test")


@pytest.fixture
def admin_user(db: None) -> User:
    return User.objects.create_superuser("admin", password="secret-pass-123")


@pytest.fixture
def admin_client(admin_user: User) -> Client:
    client = Client()
    client.force_login(admin_user)
    return client


def _act(client: Client, model: str, action: str, ids: list[int], **extra: str) -> object:
    return client.post(
        f"/admin/marceline/{model}/",
        {"action": action, "_selected_action": [str(pk) for pk in ids], **extra},
    )


def test_an_action_asks_for_confirmation_before_changing_anything(
    admin_client: Client, organisation: Organisation, member: User
) -> None:
    work = declared(organisation, SHA, member)

    page = _act(admin_client, "workrights", "to_documents_received", [work.pk])

    assert page.status_code == 200  # type: ignore[attr-defined]
    assert b'name="reason"' in page.content  # type: ignore[attr-defined]
    work.refresh_from_db()
    assert work.status == "self_declared"

    without_reason = _act(
        admin_client, "workrights", "to_documents_received", [work.pk], apply="1", reason=""
    )
    assert without_reason.status_code == 200  # type: ignore[attr-defined]
    work.refresh_from_db()
    assert work.status == "self_declared"


def test_confirmed_actions_move_the_record_and_log_the_staff_account(
    admin_client: Client, organisation: Organisation, member: User
) -> None:
    work = declared(organisation, SHA, member)

    done = _act(
        admin_client,
        "workrights",
        "to_documents_received",
        [work.pk],
        apply="1",
        reason="licence excerpt p. 2",
    )
    assert done.status_code == 302  # type: ignore[attr-defined]
    _act(
        admin_client,
        "workrights",
        "to_documents_complete",
        [work.pk],
        apply="1",
        reason="parties, work, date and signature present",
    )
    _act(admin_client, "workrights", "to_disputed", [work.pk], apply="1", reason="studio objects")

    work.refresh_from_db()
    assert work.status == "disputed"
    last = WorkRightsTransition.objects.filter(work=work).last()
    assert last is not None
    assert (last.actor_kind, last.actor_label, last.reason) == ("staff", "admin", "studio objects")


def test_a_refused_transition_is_reported_not_applied(
    admin_client: Client, organisation: Organisation, member: User
) -> None:
    work = declared(organisation, SHA, member)

    response = admin_client.post(
        "/admin/marceline/workrights/",
        {
            "action": "to_documents_complete",
            "_selected_action": [work.pk],
            "apply": "1",
            "reason": "x",
        },
        follow=True,
    )

    assert b"not allowed" in response.content
    work.refresh_from_db()
    assert work.status == "self_declared"


def test_competing_claim_and_supersede_from_the_admin(
    admin_client: Client, organisation: Organisation, member: User
) -> None:
    work = declared(organisation, SHA, member)
    other = make_organisation("Performer")

    _act(
        admin_client,
        "workrights",
        "record_competing_claim",
        [work.pk],
        apply="1",
        reason="letter of 2026-09-17",
        organisation=str(other.pk),
    )
    work.refresh_from_db()
    assert work.status == "disputed"
    assert WorkRights.objects.get(organisation=other).status == "disputed"

    _act(admin_client, "workrights", "supersede", [work.pk], apply="1", reason="settled")
    work.refresh_from_db()
    assert work.status == "superseded"
    assert services.current_work(organisation, SHA) is not None


def test_identity_verification_needs_method_and_note(
    admin_client: Client, organisation: Organisation
) -> None:
    claimant = services.claimant_for(organisation)

    _act(
        admin_client,
        "rightsclaimant",
        "verify_identity",
        [claimant.pk],
        apply="1",
        method="company_email",
        evidence_note="login mail on studio.example confirmed",
    )

    claimant = RightsClaimant.objects.get(pk=claimant.pk)
    assert claimant.identity_status == "verified"
    assert claimant.identity_method == "company_email"
    assert RightsEvent.objects.filter(kind="identity_verified", actor_label="admin").exists()


def test_revoking_a_mandate_and_verifying_a_schedule(
    admin_client: Client, organisation: Organisation, member: User
) -> None:
    agency_mandate = mandate(organisation)
    under_mandate = declared(organisation, "e" * 64, member)
    WorkRights.objects.filter(pk=under_mandate.pk).update(mandate=agency_mandate)
    schedule = RightsSchedule.objects.create(organisation=organisation, name="Catalogue")
    on_schedule = declared(organisation, "f" * 64, member)
    WorkRights.objects.filter(pk=on_schedule.pk).update(schedule=schedule)

    _act(admin_client, "agencymandate", "revoke", [agency_mandate.pk], apply="1", reason="ended")
    _act(admin_client, "rightsschedule", "verify_works", [schedule.pk], apply="1", reason="sched 1")

    agency_mandate = AgencyMandate.objects.get(pk=agency_mandate.pk)
    assert agency_mandate.revoked_at is not None
    assert WorkRights.objects.get(pk=under_mandate.pk).status == "revoked"
    assert WorkRights.objects.get(pk=on_schedule.pk).status == "documents_complete"


def test_logs_are_read_only_and_nothing_is_deletable(
    admin_client: Client, organisation: Organisation, member: User
) -> None:
    work = declared(organisation, SHA, member)
    row = work.transitions.first()
    assert row is not None

    assert admin_client.get(
        f"/admin/marceline/workrightstransition/{row.pk}/delete/"
    ).status_code in (
        403,
        302,
    )
    assert admin_client.get(f"/admin/marceline/workrights/{work.pk}/delete/").status_code in (
        403,
        302,
    )
    assert admin_client.get("/admin/marceline/workrightstransition/add/").status_code == 403


def test_mandate_links_must_stay_in_the_organisation(
    admin_client: Client, organisation: Organisation, member: User
) -> None:
    work = declared(organisation, SHA, member)
    foreign = mandate(make_organisation("Elsewhere"))

    response = admin_client.post(
        f"/admin/marceline/workrights/{work.pk}/change/",
        {
            "mandate": str(foreign.pk),
            "schedule": "",
            "transitions-TOTAL_FORMS": "0",
            "transitions-INITIAL_FORMS": "0",
        },
    )

    assert response.status_code == 200
    assert b"Belongs to another organisation." in response.content
    work.refresh_from_db()
    assert work.mandate_id is None


def test_proof_download_is_staff_only_and_logged(
    settings: Settings,
    tmp_path: Path,
    admin_client: Client,
    organisation: Organisation,
    member: User,
) -> None:
    configure_vault(settings, tmp_path / "vault")
    proof = services.upload_proof(
        organisation, PDF, filename="v.pdf", kind="licence", actor=services.Actor.customer(member)
    )
    url = f"/admin/marceline/proofdocument/{proof.pk}/download/"

    anonymous = Client().get(url)
    customer = Client()
    customer.force_login(member)
    as_customer = customer.get(url)
    unprivileged = User.objects.create_user("intern", password="secret-pass-123", is_staff=True)
    intern = Client()
    intern.force_login(unprivileged)

    assert anonymous.status_code == 302 and "/login/" in anonymous["Location"]
    assert as_customer.status_code == 302
    assert intern.get(url).status_code == 404
    assert not RightsEvent.objects.filter(kind="proof_downloaded").exists()

    response = admin_client.get(url)

    assert response.status_code == 200
    assert response.content == PDF
    assert response["Content-Disposition"] == f'attachment; filename="proof-{proof.pk}.pdf"'
    assert "no-store" in response["Cache-Control"]
    assert RightsEvent.objects.filter(kind="proof_downloaded", actor_label="admin").count() == 1
    assert admin_client.get("/admin/marceline/proofdocument/999999/download/").status_code == 404
    assert ProofDocument.objects.count() == 1
