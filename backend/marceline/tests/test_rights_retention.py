"""Retention of closed rights records (ADR 0034): end of the third calendar year, unless held."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from marceline.rights import services, vault
from marceline.rights.models import (
    AgencyMandate,
    ProofDocument,
    RightsEvent,
    WorkRights,
    WorkRightsTransition,
)
from marceline.rights.retention import apply_retention, deletable_from
from marceline.tests.app_helpers import add_member, make_organisation
from marceline.tests.rights_helpers import (
    PDF,
    configure_vault,
    documents_complete,
    mandate,
    staff_user,
)


def test_deletable_after_the_end_of_the_third_following_year() -> None:
    assert deletable_from(date(2026, 1, 1)) == date(2030, 1, 1)
    assert deletable_from(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == date(2030, 1, 1)


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "member@example.test")


def _closed_work(organisation: Organisation, sha: str, staff: User) -> WorkRights:
    work = documents_complete(organisation, sha, staff)
    return services.transition(work, "revoked", services.Actor.staff(staff), reason="rights ended")


def test_closed_records_go_with_their_log_and_proofs_after_the_period(
    settings: Settings, tmp_path: Path, organisation: Organisation, member: User
) -> None:
    configure_vault(settings, tmp_path / "vault")
    staff = staff_user()
    closed = _closed_work(organisation, "1" * 64, staff)
    proof = services.upload_proof(
        organisation,
        PDF,
        filename="c.pdf",
        kind="licence",
        work=closed,
        actor=services.Actor.customer(member),
    )
    services.read_proof(proof, services.Actor.staff(staff))
    held = _closed_work(organisation, "2" * 64, staff)
    WorkRights.objects.filter(pk=held.pk).update(legal_hold=True)
    open_work = documents_complete(organisation, "3" * 64, staff)
    year = closed.status_changed_at.year

    early = apply_retention(date(year + 3, 12, 31))
    assert early.works == []

    preview = apply_retention(date(year + 4, 1, 1), dry_run=True)
    assert preview.works == [closed.pk] and WorkRights.objects.filter(pk=closed.pk).exists()

    result = apply_retention(date(year + 4, 1, 1))

    assert result.works == [closed.pk]
    assert result.proofs == [proof.pk]
    assert result.held == [f"work {held.pk}"]
    assert not WorkRights.objects.filter(pk=closed.pk).exists()
    assert not WorkRightsTransition.objects.filter(work_id=closed.pk).exists()
    assert not ProofDocument.objects.exists()
    assert not RightsEvent.objects.filter(kind="proof_downloaded").exists()
    with pytest.raises(vault.VaultError):
        vault.read(proof.storage_name, proof.sha256)
    assert WorkRights.objects.filter(pk__in=[held.pk, open_work.pk]).count() == 2
    assert apply_retention(date(year + 4, 1, 1)).works == []  # idempotent


def test_a_revoked_mandate_stays_while_a_kept_record_points_at_it(
    organisation: Organisation,
) -> None:
    staff = staff_user()
    revoked = mandate(organisation)
    in_use = mandate(organisation)
    work = documents_complete(organisation, "4" * 64, staff)
    WorkRights.objects.filter(pk=work.pk).update(mandate=in_use, legal_hold=True)
    for row in (revoked, in_use):
        services.revoke_mandate(
            row,
            reason="ended",
            actor=services.Actor.staff(staff),
            at=datetime(2026, 3, 1, tzinfo=UTC),
        )
    running = mandate(organisation, valid_until=date(2031, 1, 1))

    result = apply_retention(date(2030, 1, 1))

    assert result.mandates == [revoked.pk]
    assert set(AgencyMandate.objects.values_list("pk", flat=True)) == {in_use.pk, running.pk}
