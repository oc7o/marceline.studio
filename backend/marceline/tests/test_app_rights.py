"""Customer rights API (ADR 0034): scoped, codes only, 404 byte-identical, development-only."""

from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from core import public_ids
from marceline.public_surface import role_allows
from marceline.rights import services
from marceline.rights.declarations import (
    CURRENT_AUTHORITY_DECLARATION,
    CURRENT_AUTHORITY_WITHDRAWAL,
    CURRENT_PARTNER_DECLARATION,
    CURRENT_WITHDRAWAL,
    CURRENT_WORK_DECLARATION,
)
from marceline.rights.models import ProofDocument, RightsEvent, WorkRights
from marceline.tests.app_helpers import (
    APP,
    UNKNOWN_ORIGINAL,
    add_member,
    client_for,
    make_organisation,
    make_reference,
    public_id_of,
)
from marceline.tests.rights_helpers import PDF, configure_vault

RIGHTS = f"{APP}/rights"


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "member@example.test")


@pytest.fixture
def client(member: User) -> Client:
    return client_for(member)


def _declaration(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "capacity": "owner",
        "asserted_right": "film_producer_right",
        "declaration_version": CURRENT_WORK_DECLARATION.version,
        "language": "en",
        "confirmed": True,
    }
    body.update(overrides)
    return body


def test_the_overview_lists_owned_originals_with_their_blockers(
    client: Client, member: User
) -> None:
    reference = make_reference(member, title="Scene one")
    make_reference(None, title="Nobody's")

    body = client.get(RIGHTS).json()

    assert body["claimant"] == {
        "claimant_type": None,
        "identity_status": "unverified",
        "identity_method": None,
        "account_control_token": None,
    }
    assert body["work_declaration"]["version"] == CURRENT_WORK_DECLARATION.version
    assert body["work_declaration"]["de"].startswith("Ich erkläre")
    assert body["partner_declaration"]["version"] == CURRENT_PARTNER_DECLARATION.version
    assert [work["original_id"] for work in body["works"]] == [public_id_of(reference)]
    assert body["works"][0]["rights_status"] == "unverified"
    assert body["works"][0]["notice_blockers"] == [
        "claimant_type_missing",
        "identity_not_verified",
        "rights_record_missing",
        "partners_not_affirmed",
    ]
    assert body["proof_upload"]["available"] is False
    assert "verified_infringement" not in str(body)


def test_claimant_type_declaration_and_partners_round_trip(client: Client, member: User) -> None:
    reference = make_reference(member)

    client.put(
        f"{RIGHTS}/claimant", {"claimant_type": "studio_producer"}, content_type="application/json"
    )
    declared = client.post(
        f"{RIGHTS}/works/{public_id_of(reference)}/declaration",
        _declaration(language="de"),
        content_type="application/json",
    )
    none = client.post(
        f"{RIGHTS}/partners/none",
        {
            "declaration_version": CURRENT_PARTNER_DECLARATION.version,
            "language": "en",
            "confirmed": True,
        },
        content_type="application/json",
    )

    assert declared.status_code == 200
    work = declared.json()["works"][0]
    assert work["rights_status"] == "self_declared"
    assert work["asserted_right"] == "film_producer_right"
    assert work["declaration_version"] == CURRENT_WORK_DECLARATION.version
    assert none.status_code == 200
    assert none.json()["partners_none_affirmed_at"] is not None
    assert none.json()["works"][0]["notice_blockers"] == [
        "identity_not_verified",
        "documents_not_complete",
    ]
    assert WorkRights.objects.get().declaration_text == CURRENT_WORK_DECLARATION.text("de")

    added = client.post(
        f"{RIGHTS}/partners",
        {"kind": "platform", "name": "Our shop", "domain": "https://www.shop.example/"},
        content_type="application/json",
    )
    assert added.status_code == 200
    partner = added.json()["partners"][0]
    assert partner["domain"] == "shop.example"
    assert added.json()["partners_none_affirmed_at"] is None

    removed = client.delete(f"{RIGHTS}/partners/{partner['id']}")
    assert removed.status_code == 200
    assert removed.json()["partners"] == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"confirmed": False}, "declaration_not_confirmed"),
        ({"declaration_version": "work-2000-01-01"}, "declaration_version_outdated"),
        ({"capacity": "authorised_agent"}, "underlying_owner_required"),
    ],
)
def test_refused_declarations_answer_409_with_a_code(
    client: Client, member: User, overrides: dict[str, object], code: str
) -> None:
    reference = make_reference(member)

    response = client.post(
        f"{RIGHTS}/works/{public_id_of(reference)}/declaration",
        _declaration(**overrides),
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json() == {"detail": code}


def test_invalid_partner_domain_is_409(client: Client) -> None:
    response = client.post(
        f"{RIGHTS}/partners",
        {"kind": "domain", "name": "Shop", "domain": "not a domain"},
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "invalid_domain"}


def test_foreign_and_unknown_ids_are_byte_identical_404s(
    settings: Settings, tmp_path: Path, client: Client, member: User
) -> None:
    configure_vault(settings, tmp_path / "vault")
    other = make_organisation("Other Studio")
    other_member = add_member(other, "other@example.test")
    foreign_reference = make_reference(other_member)
    foreign_partner = services.add_partner(
        other,
        services.PartnerInput(kind="domain", name="Theirs", domain="theirs.example"),
        services.Actor.customer(other_member),
    )

    foreign = client.post(
        f"{RIGHTS}/works/{public_id_of(foreign_reference)}/declaration",
        _declaration(),
        content_type="application/json",
    )
    unknown = client.post(
        f"{RIGHTS}/works/{UNKNOWN_ORIGINAL}/declaration",
        _declaration(),
        content_type="application/json",
    )
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.content == unknown.content

    foreign_delete = client.delete(f"{RIGHTS}/partners/{foreign_partner.public_id}")
    unknown_delete = client.delete(f"{RIGHTS}/partners/{public_ids.missing('partner')}")
    assert foreign_delete.status_code == unknown_delete.status_code == 404
    assert foreign_delete.content == unknown_delete.content

    foreign_upload = client.post(
        f"{RIGHTS}/proofs",
        {
            "file": SimpleUploadedFile("c.pdf", PDF, "application/pdf"),
            "kind": "licence",
            "original_id": public_id_of(foreign_reference),
        },
    )
    unknown_upload = client.post(
        f"{RIGHTS}/proofs",
        {
            "file": SimpleUploadedFile("c.pdf", PDF, "application/pdf"),
            "kind": "licence",
            "original_id": UNKNOWN_ORIGINAL,
        },
    )
    assert foreign_upload.status_code == unknown_upload.status_code == 404
    assert foreign_upload.content == unknown_upload.content
    assert WorkRights.objects.filter(organisation=other).count() == 0
    assert not ProofDocument.objects.exists()
    assert public_id_of(foreign_reference) not in [
        w["original_id"] for w in client.get(RIGHTS).json()["works"]
    ]


def test_upload_goes_to_the_vault_and_is_listed_without_a_download(
    settings: Settings, tmp_path: Path, client: Client, member: User
) -> None:
    configure_vault(settings, tmp_path / "vault")
    reference = make_reference(member)

    response = client.post(
        f"{RIGHTS}/proofs",
        {
            "file": SimpleUploadedFile("Lizenz.pdf", PDF, "application/pdf"),
            "kind": "licence",
            "original_id": public_id_of(reference),
            "note": "page 3",
        },
    )

    assert response.status_code == 200
    [proof] = response.json()["proofs"]
    assert proof["original_id"] == public_id_of(reference)
    assert proof["filename"] == "Lizenz.pdf"
    assert set(proof) == {
        "id",
        "kind",
        "original_id",
        "filename",
        "content_type",
        "size_bytes",
        "uploaded_at",
    }
    stored = ProofDocument.objects.get()
    assert (tmp_path / "vault" / f"{stored.storage_name}.bin").read_bytes() != PDF
    assert RightsEvent.objects.filter(kind="proof_uploaded").count() == 1
    assert WorkRights.objects.get().status == "unverified"  # an upload changes no status


@pytest.mark.parametrize(
    ("data", "status", "code"),
    [
        (b"\x00\x00\x00\x18ftypisom", 415, "unsupported_type"),
        (PDF + b"x" * 64, 413, "too_large"),
    ],
)
def test_upload_refusals(
    settings: Settings,
    tmp_path: Path,
    client: Client,
    member: User,
    data: bytes,
    status: int,
    code: str,
) -> None:
    configure_vault(settings, tmp_path / "vault")
    settings.RIGHTS_PROOF_MAX_BYTES = len(PDF) + 10
    reference = make_reference(member)

    response = client.post(
        f"{RIGHTS}/proofs",
        {
            "file": SimpleUploadedFile("clip.pdf", data, "application/pdf"),
            "kind": "other",
            "original_id": public_id_of(reference),
        },
    )

    assert response.status_code == status
    assert response.json() == {"detail": code}
    assert not ProofDocument.objects.exists()
    assert not WorkRights.objects.exists()  # a refused upload leaves no record behind


def test_upload_without_a_vault_is_503(client: Client) -> None:
    response = client.post(
        f"{RIGHTS}/proofs",
        {"file": SimpleUploadedFile("c.pdf", PDF, "application/pdf"), "kind": "licence"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "unavailable"}


def test_claimant_type_locks_after_staff_checked_documents(client: Client, member: User) -> None:
    reference = make_reference(member)
    client.put(f"{RIGHTS}/claimant", {"claimant_type": "agency"}, content_type="application/json")
    staff = User.objects.create_user("reviewer", password="secret-pass-123", is_staff=True)
    work = services.open_record(
        member.memberships.get().organisation, reference.sha256, services.SYSTEM
    )
    services.transition(work, "documents_received", services.Actor.staff(staff), reason="mandate")

    response = client.put(
        f"{RIGHTS}/claimant", {"claimant_type": "studio_producer"}, content_type="application/json"
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "claimant_type_locked"}


def test_token_is_issued_to_the_caller_only(client: Client, organisation: Organisation) -> None:
    token = client.post(f"{RIGHTS}/claimant/token").json()["claimant"]["account_control_token"]

    assert token.startswith("marceline-")
    other = add_member(make_organisation("Other"), "other@example.test")
    assert client_for(other).get(RIGHTS).json()["claimant"]["account_control_token"] is None


def test_without_organisation_or_session(db: None) -> None:
    staff = User.objects.create_user("staff", password="secret-pass-123", is_staff=True)

    assert Client().get(RIGHTS).status_code == 401
    assert client_for(staff).get(RIGHTS).status_code == 404


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("dev", True), ("prod", True)],
)
def test_rights_reach_production_but_proofs_stay_development_only(role: str, allowed: bool) -> None:
    for path in (RIGHTS, f"{RIGHTS}/works/1/declaration"):
        assert role_allows(role, path) is allowed
    assert role_allows("dev", f"{RIGHTS}/proofs") is True
    assert role_allows("prod", f"{RIGHTS}/proofs") is False


def test_production_refuses_proof_uploads_like_the_other_development_only_routes(
    settings: Settings, client: Client
) -> None:
    settings.MARCELINE_ROLE = "prod"

    proofs = client.post(f"{RIGHTS}/proofs")
    unknown = client.get(f"{APP}/invoices")  # the other development-only prefix

    assert proofs.status_code == unknown.status_code == 404
    assert proofs.content == unknown.content


def test_primary_keys_and_malformed_ids_are_the_same_404(client: Client, member: User) -> None:
    reference = make_reference(member)
    partner = services.add_partner(
        member.memberships.get().organisation,
        services.PartnerInput(kind="domain", name="Ours", domain="ours.example"),
        services.Actor.customer(member),
    )
    unknown = client.post(
        f"{RIGHTS}/works/{UNKNOWN_ORIGINAL}/declaration",
        _declaration(),
        content_type="application/json",
    )

    for original_id in (str(reference.pk), "orig_nope", public_id_of(reference).upper()):
        response = client.post(
            f"{RIGHTS}/works/{original_id}/declaration",
            _declaration(),
            content_type="application/json",
        )
        assert (response.status_code, response.content) == (404, unknown.content)
    by_pk = client.delete(f"{RIGHTS}/partners/{partner.pk}")
    assert by_pk.status_code == 404
    assert client.get(RIGHTS).json()["partners"][0]["id"] == partner.public_id


def test_withdrawing_a_self_declaration(client: Client, member: User) -> None:
    reference = make_reference(member)
    works = f"{RIGHTS}/works/{public_id_of(reference)}"
    client.post(f"{works}/declaration", _declaration(), content_type="application/json")
    body = {
        "reason": "declared_in_error",
        "declaration_version": CURRENT_WITHDRAWAL.version,
        "language": "de",
        "confirmed": True,
    }

    withdrawn = client.post(f"{works}/withdrawal", body, content_type="application/json")
    again = client.post(f"{works}/withdrawal", body, content_type="application/json")

    assert withdrawn.status_code == 200
    assert withdrawn.json()["works"][0]["rights_status"] == "unverified"
    assert withdrawn.json()["works"][0]["declared_at"] is None
    assert withdrawn.json()["withdrawal_declaration"]["version"] == CURRENT_WITHDRAWAL.version
    assert again.status_code == 409
    assert again.json() == {"detail": "declaration_not_withdrawable"}
    foreign = client.post(
        f"{RIGHTS}/works/{UNKNOWN_ORIGINAL}/withdrawal", body, content_type="application/json"
    )
    assert foreign.status_code == 404


def test_declaring_and_withdrawing_authority_over_the_documents(
    client: Client, organisation: Organisation, member: User
) -> None:
    """The customer's own statement about their documents (ADR 0034 amendment 2026-09-18): the
    only route to it is this endpoint, and only once staff recorded the documents as complete."""
    reference = make_reference(member)
    works = f"{RIGHTS}/works/{public_id_of(reference)}"
    client.post(f"{works}/declaration", _declaration(), content_type="application/json")
    body = {
        "declaration_version": CURRENT_AUTHORITY_DECLARATION.version,
        "language": "de",
        "confirmed": True,
    }

    too_early = client.post(f"{works}/authority", body, content_type="application/json")
    assert too_early.status_code == 409
    assert too_early.json() == {"detail": "authority_not_declarable"}

    staff = User.objects.create_user("reviewer", password="secret-pass-123", is_staff=True)
    actor = services.Actor.staff(staff)
    work = services.current_work(organisation, reference.sha256)
    assert work is not None
    work = services.transition(work, "documents_received", actor, reason="excerpt received")
    services.transition(work, "documents_complete", actor, reason="all fields present")

    declared_authority = client.post(f"{works}/authority", body, content_type="application/json")
    assert declared_authority.status_code == 200
    entry = declared_authority.json()["works"][0]
    assert entry["rights_status"] == "authority_declared"
    assert entry["authority_declaration_version"] == CURRENT_AUTHORITY_DECLARATION.version
    assert entry["authority_declared_at"] is not None
    assert entry["notice_blockers"] == [
        "claimant_type_missing",
        "identity_not_verified",
        "partners_not_affirmed",
    ]
    # Stored verbatim, in the language it was shown in.
    assert WorkRights.objects.get().authority_declaration_text == (
        CURRENT_AUTHORITY_DECLARATION.text("de")
    )
    stale = client.post(
        f"{works}/authority",
        {**body, "declaration_version": "authority-2019-01-01"},
        content_type="application/json",
    )
    assert stale.status_code == 409
    assert stale.json() == {"detail": "declaration_version_outdated"}

    withdrawal = {
        "reason": "authority_ended",
        "declaration_version": CURRENT_AUTHORITY_WITHDRAWAL.version,
        "language": "de",
        "confirmed": True,
    }
    withdrawn = client.post(
        f"{works}/authority/withdrawal", withdrawal, content_type="application/json"
    )
    again = client.post(
        f"{works}/authority/withdrawal", withdrawal, content_type="application/json"
    )

    assert withdrawn.status_code == 200
    withdrawn_entry = withdrawn.json()["works"][0]
    assert withdrawn_entry["rights_status"] == "documents_complete"
    assert withdrawn_entry["authority_declared_at"] is None
    assert "authority_not_declared" in withdrawn_entry["notice_blockers"]
    assert again.status_code == 409
    assert again.json() == {"detail": "authority_not_withdrawable"}
    foreign = client.post(
        f"{RIGHTS}/works/{UNKNOWN_ORIGINAL}/authority", body, content_type="application/json"
    )
    assert foreign.status_code == 404
