"""Production's copy of customer accounts (ADR 0028, "Accounts, sessions and other writes").

Development is authoritative for users, organisations and memberships (the admin and
`changepassword`, ADR 0023). Production pulls a signed accounts document (`manage.py pull_accounts`,
every 15 minutes from cron, runbook) and makes its tables match it, so **logins work while
development is down** and never trigger a request to development.

Applying is a full replacement of what the document covers: organisations, users with a
membership, and memberships, keyed by development's primary keys so a session that names a user id
keeps naming the same person. An organisation's public id (ADR 0033) is copied too, so `/app/me`
names it on production exactly as development does. A user development no longer exports is
deleted here, which ends their sessions on the next request. Sessions, login attempts and lockouts
are production's own and are never touched. Nothing here ever creates a staff or superuser account.

Since ADR 0052 slice 2 the document also carries the **open invites** — the token's digest, the
expiry, the address the account will have and the organisation it leads into — so the public host
can refuse an unknown, expired or revoked token itself and needs no way to ask development anything.
An invite revoked on development disappears from the next document, so the worst case is one pull
cycle in which a revoked token is still accepted; the write is then refused when development applies
it. Revocation is an operational control, not a security one (ADR 0052 §9).

Known limit (phase 1): two users who swap usernames between two pulls fail the unique constraint
and the pull is refused as a whole; the next pull after either rename succeeds.
"""

import logging
from datetime import datetime
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from accounts.models import (
    AccountActionToken,
    CustomerInvite,
    DocumentAcceptance,
    EmailVerification,
    Membership,
    Organisation,
)
from core import public_ids
from delivery import client
from delivery.envelope import FORMAT_ACCOUNTS, EnvelopeError, open_envelope
from delivery.models import ReplicatedAccounts
from delivery.store import CLOCK_SKEW, verification_key
from marceline.app.schemas import MonitoredSources

logger = logging.getLogger("marceline.cache")

ORGANISATION_PUBLIC_ID = "^" + public_ids.pattern("org") + "$"


class OrganisationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    public_id: str = Field(pattern=ORGANISATION_PUBLIC_ID)
    name: str = Field(max_length=200)
    segment: Literal["professional", "enterprise"]
    created_at: AwareDatetime


class UserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(max_length=128)
    is_active: bool
    organisation_id: int
    role: Literal["owner", "member"]
    membership_created_at: AwareDatetime
    #: When this address was confirmed, or null. Absent from a document written before ADR 0054,
    #: and from every document while `ACCOUNT_EMAIL_FLOWS_ENABLED` is off on development — null
    #: then, and the app's banner is hidden by `can_verify_email` anyway. The **moment**, not a
    #: flag, so production holds the fact development holds rather than the time of some pull.
    email_verified_at: AwareDatetime | None = None


class InviteIn(BaseModel):
    """One open invite, so production can refuse a token itself (ADR 0052 §9).

    The **digest** of the token, never the token: production compares digests exactly as development
    does, and a document that leaked would hand nobody a working link. No note, no issuer, no
    declaration — production only has to recognise the link and carry the redemption to the outbox.
    """

    model_config = ConfigDict(extra="forbid")

    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    email: str = Field(min_length=1, max_length=254)
    expires_at: AwareDatetime
    organisation_id: int = Field(default=0, ge=0)
    organisation_name: str = Field(default="", max_length=200)


class ActionTokenIn(BaseModel):
    """One mailed link production must be able to answer for (ADR 0054 §1).

    The **digest** of the token, never the token: production compares digests exactly as development
    does, so a document that leaked would hand nobody a working link. No address either — production
    reads it off the user row this names.
    """

    model_config = ConfigDict(extra="forbid")

    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["password_reset", "email_verification"]
    user_id: int
    expires_at: AwareDatetime


class AccountsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["marceline.accounts"]
    schema_version: Literal[1]
    as_of: AwareDatetime
    expires_at: AwareDatetime
    monitored_sources: MonitoredSources
    organisations: list[OrganisationIn]
    users: list[UserIn]
    #: Absent from a document written before ADR 0052 slice 2, and from every document while
    #: `ONBOARDING_INVITES_ENABLED` is off on development: an empty list then, and production's
    #: invite routes answer the one neutral 404 for every token.
    invites: list[InviteIn] = Field(default_factory=list)
    #: Absent from a document written before ADR 0054, and while the flows are off on development.
    action_tokens: list[ActionTokenIn] = Field(default_factory=list)


def verify_accounts(wire: bytes, key: Ed25519PublicKey, *, now: datetime) -> AccountsPayload:
    raw = open_envelope(wire, key, expected_format=FORMAT_ACCOUNTS)
    try:
        payload = AccountsPayload.model_validate(raw)
    except ValidationError as exc:
        raise EnvelopeError("invalid_payload") from exc
    if payload.as_of > now + CLOCK_SKEW:
        raise EnvelopeError("from_the_future")
    if payload.expires_at <= now:
        raise EnvelopeError("expired")
    organisation_ids = {row.id for row in payload.organisations}
    if any(user.organisation_id not in organisation_ids for user in payload.users):
        raise EnvelopeError("unknown_organisation")
    user_ids = {row.id for row in payload.users}
    for token in payload.action_tokens:
        if token.user_id not in user_ids:
            # A link for an account the document does not carry could never be answered here, and a
            # token row without its user cannot even be stored (the FK).
            raise EnvelopeError("unknown_account")
    for invite in payload.invites:
        # The same rule the database keeps on development (`customer_invite_one_destination`): an
        # existing organisation **or** a name to create one under, never both and never neither.
        if bool(invite.organisation_id) == bool(invite.organisation_name):
            raise EnvelopeError("invalid_payload")
        if invite.organisation_id and invite.organisation_id not in organisation_ids:
            raise EnvelopeError("unknown_organisation")
    return payload


def apply_accounts(payload: AccountsPayload, *, now: datetime) -> None:
    """Make production's accounts match the document. Refuses a document older than the last."""
    try:
        with transaction.atomic():
            state = ReplicatedAccounts.objects.select_for_update().first()
            if state is not None and payload.as_of < state.as_of:
                raise EnvelopeError("as_of_went_backwards")
            user_ids = {row.id for row in payload.users}
            organisation_ids = {row.id for row in payload.organisations}
            # Production never holds anyone development did not export: no staff, no strays.
            User.objects.exclude(pk__in=user_ids).delete()
            Membership.objects.exclude(user_id__in=user_ids).delete()
            Organisation.objects.exclude(pk__in=organisation_ids).delete()
            for organisation in payload.organisations:
                Organisation.objects.update_or_create(
                    pk=organisation.id,
                    defaults={
                        "name": organisation.name,
                        "segment": organisation.segment,
                        "public_id": organisation.public_id,
                    },
                )
            Organisation.objects.bulk_update(
                [
                    Organisation(pk=row.id, created_at=row.created_at)
                    for row in payload.organisations
                ],
                ["created_at"],
            )
            for row in payload.users:
                User.objects.update_or_create(
                    pk=row.id,
                    defaults={
                        "username": row.username,
                        "password": row.password,
                        "is_active": row.is_active,
                        "is_staff": False,
                        "is_superuser": False,
                    },
                )
                membership, _ = Membership.objects.update_or_create(
                    user_id=row.id,
                    defaults={"organisation_id": row.organisation_id, "role": row.role},
                )
                if membership.created_at != row.membership_created_at:
                    Membership.objects.filter(pk=membership.pk).update(
                        created_at=row.membership_created_at
                    )
            _apply_invites(payload.invites)
            _apply_action_tokens(payload.action_tokens)
            _apply_verification(payload.users)
            ReplicatedAccounts.objects.update_or_create(
                singleton=True,
                defaults={
                    "as_of": payload.as_of,
                    "applied_at": now,
                    "monitored_sources": payload.monitored_sources,
                },
            )
    except IntegrityError as exc:
        raise EnvelopeError("conflicting_accounts") from exc


def _apply_action_tokens(rows: list[ActionTokenIn]) -> None:
    """Make production's mailed links match the document (ADR 0054 §1).

    A full replacement of the open set, with the same two rules as the invites: only the replicated
    fields are written, so a link this host has already handed to the outbox keeps its
    `handed_over_at` and stays finished; and a token that left the document — used, expired or
    replaced by a newer one — is deleted, so it answers the one neutral 404 from then on.
    """
    keep = [row.token_hash for row in rows]
    AccountActionToken.objects.exclude(token_hash__in=keep).delete()
    for row in rows:
        AccountActionToken.objects.update_or_create(
            token_hash=row.token_hash,
            defaults={
                "kind": row.kind,
                "user_id": row.user_id,
                "expires_at": row.expires_at,
                "created_at": row.expires_at,
            },
        )


def _apply_verification(rows: list[UserIn]) -> None:
    """Whether each replicated address is confirmed, so `/app/me` can answer it here (ADR 0054).

    Development is authoritative: a confirmation it does not carry is removed here, which is what
    makes an account it deleted and recreated honest again. `last_sent_at` is never replicated — the
    resend interval belongs to the host that sends, and that host is development.
    """
    for row in rows:
        EmailVerification.objects.update_or_create(
            user_id=row.id, defaults={"verified_at": row.email_verified_at}
        )


def _apply_invites(rows: list[InviteIn]) -> None:
    """Make production's open invites match the document (ADR 0052 §9).

    A full replacement of the open set, with two rules that matter:

    - **local progress is never overwritten.** Only the replicated fields are written, so a creator
      halfway through a redemption keeps their password hash, their declarations and their
      acceptances across a pull — and a redemption already handed to the outbox keeps
      `handed_over_at`, which is what makes single use hold on this host.
    - **an invite that left the document is deleted**, because it was revoked, expired or redeemed:
      the link then answers the one neutral 404, which is the same answer it gets on development.
      The half-finished acceptance rows go with it — they were staging for a redemption that either
      completed (development holds the real rows) or never will.
    """
    keep = [row.token_hash for row in rows]
    stale = CustomerInvite.objects.exclude(token_hash__in=keep)
    DocumentAcceptance.objects.filter(invite__in=stale).delete()
    stale.delete()
    for row in rows:
        CustomerInvite.objects.update_or_create(
            token_hash=row.token_hash,
            defaults={
                "email": row.email,
                "expires_at": row.expires_at,
                "organisation_id": row.organisation_id or None,
                "organisation_name": row.organisation_name,
            },
        )


def pull_accounts(*, now: datetime) -> AccountsPayload:
    """Fetch, verify and apply. Raises `client.DeliveryError` or `EnvelopeError` (codes only)."""
    payload = verify_accounts(client.fetch_accounts(), verification_key(), now=now)
    apply_accounts(payload, now=now)
    logger.info(
        "accounts applied: %s organisation(s), %s user(s), %s open invite(s), %s mailed link(s)",
        len(payload.organisations),
        len(payload.users),
        len(payload.invites),
        len(payload.action_tokens),
    )
    return payload


def replicated_monitored_sources() -> MonitoredSources | None:
    """The coverage bucket the last accounts document carried, or None before the first pull."""
    state = ReplicatedAccounts.objects.first()
    return state.monitored_sources if state is not None else None  # type: ignore[return-value]
