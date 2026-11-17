"""Helpers for the customer API tests: organisations, members and originals they own."""

from collections.abc import Iterable
from itertools import count

from django.contrib.auth.models import User
from django.test import Client

from accounts.models import Membership, Organisation
from catalog.models import Creator
from core import public_ids
from marceline.app.ownership import (
    ORIGINAL_ID_PREFIX,
    OwnedOriginal,
    group_originals,
    public_ids_for,
)
from marceline.models import OriginalOwnership
from matching.models import Reference

APP = "/api/v1/app"
#: A well-formed original id that nobody owns: the "does not exist" side of every 404 comparison.
UNKNOWN_ORIGINAL = public_ids.generate(ORIGINAL_ID_PREFIX)

_digests = count(1)


def make_organisation(name: str, segment: str = Organisation.Segment.PROFESSIONAL) -> Organisation:
    return Organisation.objects.create(name=name, segment=segment)


def add_member(
    organisation: Organisation,
    username: str,
    *,
    role: str = Membership.Role.OWNER,
    is_staff: bool = False,
) -> User:
    user = User.objects.create_user(username, password="secret-pass-123", is_staff=is_staff)
    Membership.objects.create(organisation=organisation, user=user, role=role)
    return user


def client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def own(reference: Reference, organisation: Organisation) -> Reference:
    """Assign an original to an organisation, as an upload with `--organisation` would."""
    OriginalOwnership.objects.create(
        reference=reference, organisation=organisation, assigned_via=OriginalOwnership.Via.UPLOAD
    )
    public_ids_for(organisation, [reference.sha256])
    return reference


def public_id_of(reference: Reference) -> str:
    """The id customers know this (owned) original by: its file's public id in its organisation."""
    ownership = OriginalOwnership.objects.select_related("organisation").get(pk=reference.pk)
    return public_ids_for(ownership.organisation, [reference.sha256])[reference.sha256]


def grouped(references: Iterable[Reference]) -> list[OwnedOriginal]:
    """`group_originals` with a throwaway public id per file, for tests of the case sync itself."""
    rows = list(references)
    return group_originals(
        rows, {row.sha256: public_ids.generate(ORIGINAL_ID_PREFIX) for row in rows}
    )


def make_reference(
    owner: User | None,
    *,
    title: str = "An original",
    duration_s: float | None = 600.0,
    organisation: Organisation | None = None,
) -> Reference:
    """An original registered by `owner`, owned by `organisation` or else by `owner`'s own.

    Ownership is the `OriginalOwnership` row (ADR 0031); `uploaded_by` is only the audit of who
    registered it. `make_reference(None)` is an unassigned operator import.
    """
    creator, _ = Creator.objects.get_or_create(name="Synthetic Artist")
    reference = Reference.objects.create(
        creator=creator,
        title=title,
        duration_s=duration_s,
        sha256=f"{next(_digests):064d}",
        frame_fps=1,
        uploaded_by=owner,
    )
    if organisation is None and owner is not None:
        membership = Membership.objects.filter(user=owner).select_related("organisation").first()
        organisation = membership.organisation if membership is not None else None
    return own(reference, organisation) if organisation is not None else reference
