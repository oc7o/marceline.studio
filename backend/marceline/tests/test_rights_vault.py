"""The proof vault: encrypted at rest, outside the repository, documents only (ADR 0034)."""

import hashlib
import os
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from marceline.rights import services, vault
from marceline.rights.models import ProofDocument, RightsEvent
from marceline.tests.app_helpers import add_member, make_organisation
from marceline.tests.rights_helpers import PDF, PNG, configure_vault, staff_user


@pytest.fixture
def vault_dir(settings: Settings, tmp_path: Path) -> Path:
    directory = tmp_path / "vault"
    configure_vault(settings, directory)
    return directory


def test_round_trip_encrypts_at_rest(vault_dir: Path) -> None:
    blob = vault.store(PDF)

    stored = (vault_dir / f"{blob.storage_name}.bin").read_bytes()
    assert PDF not in stored and b"%PDF" not in stored
    assert blob.content_type == "application/pdf"
    assert blob.sha256 == hashlib.sha256(PDF).hexdigest()
    assert vault.read(blob.storage_name, blob.sha256) == PDF
    assert stat.S_IMODE(os.stat(vault_dir / f"{blob.storage_name}.bin").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(vault_dir).st_mode) == 0o700


def test_old_keys_still_decrypt_after_rotation(settings: Settings, vault_dir: Path) -> None:
    blob = vault.store(PNG)
    settings.RIGHTS_VAULT_KEYS = f"{Fernet.generate_key().decode()},{settings.RIGHTS_VAULT_KEYS}"

    assert vault.read(blob.storage_name, blob.sha256) == PNG


def test_a_wrong_key_or_a_swapped_file_is_refused(settings: Settings, vault_dir: Path) -> None:
    first = vault.store(PDF)
    second = vault.store(PNG)

    with pytest.raises(vault.VaultError) as swapped:
        vault.read(first.storage_name, second.sha256)
    assert swapped.value.code == "corrupt"

    settings.RIGHTS_VAULT_KEYS = Fernet.generate_key().decode()
    with pytest.raises(vault.VaultError) as wrong_key:
        vault.read(first.storage_name, first.sha256)
    assert wrong_key.value.code == "corrupt"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "empty"),
        (b"GIF89a....", "unsupported_type"),
        (b"\x00\x00\x00\x18ftypmp42", "unsupported_type"),
        (b"<html>%PDF-</html>", "unsupported_type"),
    ],
)
def test_only_documents_are_accepted(vault_dir: Path, data: bytes, code: str) -> None:
    with pytest.raises(vault.VaultError) as error:
        vault.store(data)

    assert error.value.code == code
    assert not vault_dir.exists() or not any(vault_dir.iterdir())


def test_size_limit(settings: Settings, vault_dir: Path) -> None:
    settings.RIGHTS_PROOF_MAX_BYTES = len(PDF) - 1

    with pytest.raises(vault.VaultError) as error:
        vault.store(PDF)

    assert error.value.code == "too_large"


def test_unconfigured_or_misplaced_vault_is_unavailable(settings: Settings, tmp_path: Path) -> None:
    assert vault.problems() == ["RIGHTS_VAULT_KEYS is not set", "RIGHTS_VAULT_DIR is not set"]
    with pytest.raises(vault.VaultError) as error:
        vault.store(PDF)
    assert error.value.code == "unavailable"

    settings.RIGHTS_VAULT_KEYS = Fernet.generate_key().decode()
    settings.RIGHTS_VAULT_DIR = str(vault.repository_root() / "frontend" / "dist" / "vault")
    assert vault.problems() == ["RIGHTS_VAULT_DIR must be outside the repository"]
    settings.RIGHTS_VAULT_DIR = "relative/vault"
    assert vault.problems() == ["RIGHTS_VAULT_DIR must be an absolute path"]
    settings.RIGHTS_VAULT_DIR = str(tmp_path)
    settings.RIGHTS_VAULT_KEYS = "not-a-key"
    assert vault.problems() == ["RIGHTS_VAULT_KEYS holds a value that is not a Fernet key"]


def test_a_vault_volume_is_allowed_where_the_code_is_mounted_at_the_top(
    settings: Settings,
) -> None:
    """The container case (issue #59): `backend/` is bind-mounted at `/app`, so
    the repository root the old ancestor test used "is" `/`, and testing that as an ancestor calls
    every absolute path "inside the repository" — exactly the bug fixed in the upload spool
    (`marceline.uploads.spool`), from the same `core.checkout_paths.inside_checkout` now. The code
    directory itself is still refused.
    """
    settings.RIGHTS_VAULT_KEYS = Fernet.generate_key().decode()
    settings.BASE_DIR = "/app"
    settings.RIGHTS_VAULT_DIR = "/var/lib/marceline/rights-vault"
    assert vault.problems() == []
    settings.RIGHTS_VAULT_DIR = "/app/vault"
    assert vault.problems() == ["RIGHTS_VAULT_DIR must be outside the repository"]


def test_storage_names_cannot_escape_the_directory(vault_dir: Path) -> None:
    with pytest.raises(vault.VaultError):
        vault.read("../../etc/passwd", "0" * 64)


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "member@example.test")


def test_upload_records_metadata_and_every_staff_read_is_logged(
    vault_dir: Path, organisation: Organisation, member: User
) -> None:
    proof = services.upload_proof(
        organisation,
        PDF,
        filename="../../Vertrag Auszug (1).pdf",
        kind="contract_excerpt",
        actor=services.Actor.customer(member),
    )
    staff = staff_user()

    assert proof.original_filename == "Vertrag Auszug _1_.pdf"
    assert proof.uploaded_via == "customer"
    assert services.read_proof(proof, services.Actor.staff(staff)) == PDF
    with pytest.raises(services.RightsError):
        services.read_proof(proof, services.Actor.customer(member))
    downloads = RightsEvent.objects.filter(kind="proof_downloaded", proof=proof)
    assert list(downloads.values_list("actor_label", flat=True)) == ["reviewer"]


def test_a_failed_record_leaves_no_file_behind(
    vault_dir: Path, organisation: Organisation, member: User
) -> None:
    other = make_organisation("Other")
    foreign = services.open_record(other, "c" * 64, services.SYSTEM)

    with pytest.raises(services.RightsError):
        services.upload_proof(
            organisation,
            PDF,
            filename="x.pdf",
            kind="licence",
            work=foreign,
            actor=services.Actor.customer(member),
        )

    assert not ProofDocument.objects.exists()
    assert not vault_dir.exists() or not any(vault_dir.iterdir())
