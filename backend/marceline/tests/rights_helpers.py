"""Helpers for the rights tests (ADR 0034): works, vault settings and tiny synthetic documents."""

from datetime import date
from pathlib import Path

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from marceline.rights import services
from marceline.rights.declarations import (
    CURRENT_AUTHORITY_DECLARATION,
    CURRENT_WORK_DECLARATION,
)
from marceline.rights.models import AgencyMandate, WorkRights

#: Not real documents: just the leading bytes the vault recognises, plus filler.
PDF = b"%PDF-1.4\n% synthetic test document\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def configure_vault(settings: Settings, directory: Path) -> str:
    key = Fernet.generate_key().decode()
    settings.RIGHTS_VAULT_KEYS = key
    settings.RIGHTS_VAULT_DIR = str(directory)
    return key


def staff_user(username: str = "reviewer") -> User:
    return User.objects.create_user(username, password="secret-pass-123", is_staff=True)


def declaration(**overrides: object) -> services.DeclarationInput:
    values: dict[str, object] = {
        "capacity": "owner",
        "asserted_right": "copyright",
        "declaration_version": CURRENT_WORK_DECLARATION.version,
        "language": "en",
        "confirmed": True,
    }
    values.update(overrides)
    return services.DeclarationInput(**values)  # type: ignore[arg-type]


def declared(
    organisation: Organisation, sha256: str, user: User, **overrides: object
) -> WorkRights:
    return services.self_declare(
        organisation, sha256, declaration(**overrides), services.Actor.customer(user)
    )


def documents_complete(organisation: Organisation, sha256: str, staff: User) -> WorkRights:
    """As far as staff can take a record: the documents are there and formally complete."""
    actor = services.Actor.staff(staff)
    work = services.open_record(organisation, sha256, actor)
    work = services.transition(work, "documents_received", actor, reason="excerpt received")
    return services.transition(
        work, "documents_complete", actor, reason="parties, work, date and signature present"
    )


def authority_declared(organisation: Organisation, sha256: str, user: User) -> WorkRights:
    """The customer's own statement on top of that — the only route to a clear rights gate."""
    return services.declare_authority(
        organisation,
        sha256,
        services.AuthorityInput(
            declaration_version=CURRENT_AUTHORITY_DECLARATION.version,
            language="en",
            confirmed=True,
        ),
        services.Actor.customer(user),
    )


def mandate(organisation: Organisation, **overrides: object) -> AgencyMandate:
    values: dict[str, object] = {
        "organisation": organisation,
        "principal_name": "Rights Holder GmbH",
        "agent_name": "Agency GmbH",
        "rights_covered": "exclusive right of making available",
        "authority_to_prepare_notices": True,
        "valid_from": date(2026, 1, 1),
    }
    values.update(overrides)
    return AgencyMandate.objects.create(**values)
