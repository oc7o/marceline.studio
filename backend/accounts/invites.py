"""Issuing and redeeming a customer invite (ADR 0052, issue #115).

The half that writes. `accounts/onboarding.py` holds the token's shape, the documents and the
steps; this module turns them into rows, and `marceline/app/invites.py` turns *this* into HTTP.

Two things are load-bearing and easy to lose in a refactor:

- **`lookup()` is the only way in.** It hashes what arrived and returns the row or `None`, and
  every reason for `None` — unknown, malformed, expired, revoked, redeemed, feature off — is the
  same `None`. No caller may ask which; that is the enumeration rule of ADR 0052 §6.
- **`complete()` is one transaction and the only place an account appears.** Until it runs there is
  no user, no organisation and no membership; a half-finished redemption lives entirely on the
  invite row. `select_for_update` on the invite is what makes a double submission one redemption.
  It refuses outright on production: the public host creates no account (ADR 0028), it **hands the
  redemption over** (`lock_for_handover` / `redemption_of` / `mark_handed_over`, ADR 0052 §9) and
  development applies it with `apply_redemption`, which ends in this very function.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import identify_hasher, is_password_usable, make_password
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils import timezone

from accounts import account_actions, onboarding, passwords
from accounts import business_status as business_status_rules
from accounts.declarations import (
    CURRENT_AGE_DECLARATION,
    CURRENT_BUSINESS_STATUS_DECLARATION,
)
from accounts.models import (
    BusinessStatus,
    CustomerInvite,
    DocumentAcceptance,
    Membership,
    Organisation,
)
from accounts.onboarding import REFUSED, Step

logger = logging.getLogger(__name__)


class InviteRefused(Exception):
    """Everything a caller may say about a token that does not work: `REFUSED`, and nothing else."""

    def __init__(self) -> None:
        super().__init__(REFUSED)


class AlreadyRedeemed(Exception):
    """This invite has already produced its account (ADR 0052 §9).

    Not an error: a redemption production accepted twice, or an acknowledgement that was lost, ends
    here. The outbox item is settled as applied with no effect.
    """


class AccountAlreadyExists(Exception):
    """The address on a valid invite already has an account.

    Only reachable by somebody holding the invite, and only at the last step. The endpoint answers
    `REFUSED` like every other failure — but this one is logged, because it means an invite can
    never be completed and a human has to look.
    """


def issue(
    *,
    email: str,
    organisation: Organisation | None = None,
    organisation_name: str = "",
    segment: str = "professional",
    invited_by: User | None = None,
    note: str = "",
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[CustomerInvite, str]:
    """Create an invite and return it with its token. **The token is returned once, here.**

    After this call it exists nowhere but in the caller's hands: the row keeps only the digest.
    """
    if not onboarding.invites_enabled():
        raise InviteRefused
    moment = now or timezone.now()
    token = onboarding.new_token()
    invite = CustomerInvite.objects.create(
        organisation=organisation,
        organisation_name=organisation_name.strip(),
        segment=segment,
        email=email.strip(),
        token_hash=onboarding.token_hash(token),
        invited_by=invited_by,
        note=note.strip(),
        expires_at=expires_at or moment + timedelta(days=onboarding.expiry_days()),
    )
    return invite, token


def invite_link(token: str) -> str:
    """The address the operator pastes into the message he is writing anyway (ADR 0052 §2).

    `ONBOARDING_INVITE_BASE_URL` is the customer app's origin; the path is the Next route.
    """
    base = str(getattr(settings, "ONBOARDING_INVITE_BASE_URL", "") or "").rstrip("/")
    return f"{base}/app/invite/{token}"


def usable(invite: CustomerInvite, *, now: datetime | None = None) -> bool:
    """Not revoked, not redeemed, not expired. The only predicate; never split into messages."""
    moment = now or timezone.now()
    return invite.revoked_at is None and invite.redeemed_at is None and invite.expires_at > moment


def lookup(token: str, *, now: datetime | None = None) -> CustomerInvite | None:
    """The invite this token opens, or `None` — for every reason there is, indistinguishably.

    One indexed query on the digest, and the shape check runs first so a malformed token costs a
    regex instead of a query. Both paths answer `None`, and the endpoint says `REFUSED` either way.

    A redemption **handed to the outbox** on production (ADR 0052 §9) is one of those reasons: from
    that moment the link answers the neutral page, exactly as a redeemed one does on development.
    The condition lives here rather than in `usable()` because development's apply path reads the
    row directly and must not be stopped by production's marker.
    """
    if not onboarding.invites_enabled() or not onboarding.token_well_formed(token):
        return None
    invite = CustomerInvite.objects.filter(token_hash=onboarding.token_hash(token)).first()
    if invite is None or not usable(invite, now=now) or invite.handed_over_at is not None:
        return None
    return invite


def step_of(invite: CustomerInvite) -> Step:
    """Where this redemption stands. Derived from the row — see `CustomerInvite`'s docstring.

    A declaration counts only while it is **the current wording**. A redemption that was left
    half-finished across a change to `accounts/declarations.py` therefore comes back to the
    declaration step and signs what is published today (issue #123): a superseded version is a
    record to read back, never a signature that still stands in for the new text.
    """
    if invite.redeemed_at is not None:
        return Step.DONE
    if not invite.password_hash:
        return Step.PASSWORD
    if (
        invite.declaration_version != CURRENT_BUSINESS_STATUS_DECLARATION.version
        or invite.age_declaration_version != CURRENT_AGE_DECLARATION.version
    ):
        return Step.DECLARATION
    if not invite.acceptances.exists():
        return Step.DOCUMENTS
    if not invite.country:
        return Step.COUNTRY
    return Step.COMPLETE


def set_password(invite: CustomerInvite, password: str) -> None:
    """Step one. Django's own validators decide; the hash is held on the invite until completion.

    The username is not a choice: it is the address the invite was issued to, which is what makes
    this an invitation rather than a registration.
    """
    user_model = get_user_model()
    probe = user_model(username=invite.email, email=invite.email)
    validate_password(password, user=probe)  # raises ValidationError with the messages to show
    invite.password_hash = make_password(password)
    invite.save(update_fields=["password_hash"])


def record_declarations(invite: CustomerInvite, *, now: datetime | None = None) -> None:
    """Step two: both versioned wordings, stored verbatim with their versions.

    The *Unternehmer* declaration (§ 14 BGB, issue #92) and the declaration of majority and full
    legal capacity (issue #123). They are one step because they are one screen and one moment: the
    person reads two sentences and agrees to both, and a redemption that had signed one but not the
    other would be a state nothing else in the flow could describe.

    Always the **current** version of each — the redemption presents what `accounts/declarations.py`
    says today, and an older one is only ever read back, never signed again.
    """
    moment = now or timezone.now()
    declaration = CURRENT_BUSINESS_STATUS_DECLARATION
    age = CURRENT_AGE_DECLARATION
    invite.declaration_version = declaration.version
    invite.declaration_text = declaration.text()
    invite.declared_at = moment
    invite.age_declaration_version = age.version
    invite.age_declaration_text = age.text()
    invite.age_declared_at = moment
    invite.save(
        update_fields=[
            "declaration_version",
            "declaration_text",
            "declared_at",
            "age_declaration_version",
            "age_declaration_text",
            "age_declared_at",
        ]
    )


def record_acceptances(
    invite: CustomerInvite, *, language: str, now: datetime | None = None
) -> list[DocumentAcceptance]:
    """Step three: one append-only row per published document (ADR 0052 §3).

    Raises `DocumentsNotPublished` while `ONBOARDING_DOCUMENTS` is empty or incomplete — the gate
    that keeps this flow behind the legal work rather than in front of it.
    """
    documents = onboarding.published_documents()
    moment = now or timezone.now()
    return DocumentAcceptance.objects.bulk_create(
        DocumentAcceptance(
            invite=invite,
            organisation=invite.organisation,
            document_id=document.document_id,
            version=document.version,
            sha256=document.sha256,
            url=document.url,
            language=language,
            accepted_at=moment,
        )
        for document in documents
    )


def record_country(invite: CustomerInvite, country: str) -> bool:
    """Step four. True when the pilot covers it; False ends the redemption politely.

    A country outside the EU is **counted and not stored**, and the invite is not consumed: a typo
    on a select box must not cost somebody their invitation (ADR 0052 §5).
    """
    code = country.strip().upper()
    if code in business_status_rules.EU_MEMBER_STATES:
        invite.country = code
        invite.save(update_fields=["country"])
        return True
    invite.outside_eu_answers = invite.outside_eu_answers + 1
    invite.save(update_fields=["outside_eu_answers"])
    return False


@transaction.atomic
def complete(invite: CustomerInvite, *, now: datetime | None = None, language: str = "") -> User:
    """The one transaction in which an account appears (ADR 0052 §8).

    Organisation, user, membership, business status and the acceptances' owner, then `redeemed_at`.
    The invite row is locked first, so two submissions are one redemption; the second finds
    `redeemed_at` set and is refused with the same `REFUSED` as anything else.

    `Membership.full_clean()` runs, so the issue #92 guard applies exactly as it does in the admin:
    with `ONBOARDING_BUSINESS_STATUS_GUARD=refuse` a redemption cannot grant access before a human
    has recorded the evidence, and the creator is told the account is being set up by hand.

    **Never on production.** The public host holds accounts only through `pull_accounts` (ADR 0028)
    and hands a finished redemption to the outbox instead (`hand_over`, ADR 0052 §9). The role check
    is here rather than only in the view, so no later caller can create an account there by mistake.
    """
    if settings.MARCELINE_ROLE == "prod":
        raise InviteRefused
    moment = now or timezone.now()
    locked = CustomerInvite.objects.select_for_update().get(pk=invite.pk)
    if not usable(locked, now=moment) or step_of(locked) is not Step.COMPLETE:
        raise InviteRefused

    user_model = get_user_model()
    if user_model.objects.filter(username=locked.email).exists():
        logger.error("invite %s cannot be completed: the address already has an account", locked.pk)
        raise AccountAlreadyExists

    organisation = locked.organisation
    if organisation is None:
        organisation = Organisation.objects.create(
            name=locked.organisation_name, segment=locked.segment
        )

    user = user_model.objects.create(
        username=locked.email, email=locked.email, password=locked.password_hash
    )

    membership = Membership(organisation=organisation, user=user, role=Membership.Role.OWNER)
    membership.full_clean()
    membership.save()

    BusinessStatus.objects.update_or_create(
        organisation=organisation,
        defaults={
            "country": locked.country,
            "declaration_version": locked.declaration_version,
            "declaration_text": locked.declaration_text,
            "declared_at": locked.declared_at,
            "age_declaration_version": locked.age_declaration_version,
            "age_declaration_text": locked.age_declaration_text,
            "age_declared_at": locked.age_declared_at,
        },
    )

    locked.acceptances.update(user=user, organisation=organisation)
    locked.organisation = organisation
    locked.organisation_name = ""
    locked.redeemed_at = moment
    locked.redeemed_user = user
    locked.password_hash = ""
    locked.save(
        update_fields=[
            "organisation",
            "organisation_name",
            "redeemed_at",
            "redeemed_user",
            "password_hash",
        ]
    )
    # The address has never been confirmed by whoever reads it — staff typed it (ADR 0054 §3). The
    # mail goes out after this transaction commits: a transport takes seconds and must not hold the
    # invite's row lock, and a redemption must not fail because a mail server is down.
    transaction.on_commit(lambda: account_actions.request_verification(user, language=language))
    return user


# --- production: a finished redemption travels as an outbox item (ADR 0052 §9) ------------------
#
# Production cannot create the account: it holds accounts only through `pull_accounts` and writes no
# customer data into its own tables (ADR 0028). So the last step **hands the redemption over** —
# `lock_for_handover` → `redemption_of` → the outbox → `mark_handed_over` — and development
# applies it with `apply_redemption`, which ends in `complete()`, the same transaction its own
# endpoint runs.
#
# What travels is the smallest thing that can be applied: the invite's **digest** (never the token,
# never the address — development's own row holds it), the Django password hash production made, the
# versions and moments of the two declarations, the accepted documents by id/version/digest, and the
# country. No declaration text and no document text: development holds both and re-derives them, so
# a wording that changed in between is refused rather than signed on somebody's behalf.


@dataclass(frozen=True)
class AcceptedDocument:
    """One document as it was presented on production, by id, version and digest."""

    document_id: str
    version: str
    sha256: str


@dataclass(frozen=True)
class Redemption:
    """Everything development needs to turn a redemption into an account, and nothing else."""

    token_hash: str
    password_hash: str
    declaration_version: str
    age_declaration_version: str
    declared_at: datetime
    age_declared_at: datetime
    country: str
    language: str
    accepted_at: datetime
    documents: tuple[AcceptedDocument, ...]

    def as_payload(self) -> dict[str, Any]:
        """The JSON the outbox item carries. Plain values only: `delivery` never interprets it."""
        return {
            "token_hash": self.token_hash,
            "password_hash": self.password_hash,
            "declaration_version": self.declaration_version,
            "age_declaration_version": self.age_declaration_version,
            "declared_at": self.declared_at.isoformat(),
            "age_declared_at": self.age_declared_at.isoformat(),
            "country": self.country,
            "language": self.language,
            "accepted_at": self.accepted_at.isoformat(),
            "documents": [
                {
                    "document_id": document.document_id,
                    "version": document.version,
                    "sha256": document.sha256,
                }
                for document in self.documents
            ],
        }


class RedemptionRefused(Exception):
    """Development will not apply this redemption. `code` is staff-facing and is never a sentence.

    The creator is told nothing: production has no channel to reach them (ADR 0052 §9), they find
    sign-in refusing them, and the operator finds the code on the outbox item in the admin.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def lock_for_handover(invite: CustomerInvite, *, now: datetime | None = None) -> CustomerInvite:
    """The invite, locked, when the outbox may take its redemption — or `InviteRefused`.

    Called inside the transaction that enqueues, so two submissions produce **one** item: the second
    finds `handed_over_at` set and is refused with the same `REFUSED` as every other failure.
    """
    moment = now or timezone.now()
    locked = CustomerInvite.objects.select_for_update().get(pk=invite.pk)
    if (
        not usable(locked, now=moment)
        or locked.handed_over_at is not None
        or step_of(locked) is not Step.COMPLETE
    ):
        raise InviteRefused
    return locked


def redemption_of(invite: CustomerInvite, *, now: datetime | None = None) -> Redemption:
    """What the outbox item carries for this finished redemption (production side)."""
    acceptances = list(invite.acceptances.order_by("pk"))
    moment = now or timezone.now()
    return Redemption(
        token_hash=invite.token_hash,
        password_hash=invite.password_hash,
        declaration_version=invite.declaration_version,
        age_declaration_version=invite.age_declaration_version,
        declared_at=invite.declared_at or moment,
        age_declared_at=invite.age_declared_at or moment,
        country=invite.country,
        language=acceptances[0].language if acceptances else "",
        accepted_at=acceptances[0].accepted_at if acceptances else moment,
        documents=tuple(
            AcceptedDocument(document_id=row.document_id, version=row.version, sha256=row.sha256)
            for row in acceptances
        ),
    )


def mark_handed_over(
    invite: CustomerInvite, *, submission_id: str, now: datetime | None = None
) -> None:
    """The redemption is in the outbox: this link is finished, whatever happens next.

    The password hash is dropped from the row in the same write. It has left for development, and
    production has no use for it ever again — the account it belongs to will be created there.
    """
    invite.handed_over_at = now or timezone.now()
    invite.submission_id = submission_id[:64]
    invite.password_hash = ""
    invite.save(update_fields=["handed_over_at", "submission_id", "password_hash"])


def _checked_password_hash(password_hash: str) -> str:
    """The hash, when Django recognises it as one of *its* hashers' — or `RedemptionRefused`.

    `User.password` is set from this string as it arrives, so an unrecognised value would create an
    account nobody can sign in to (or, with a `!` prefix, one with an unusable password that a later
    reset could take over). Only a hash of a configured hasher is accepted, and `PASSWORD_HASHERS`
    is the same list on both machines.
    """
    if not password_hash or not is_password_usable(password_hash):
        raise RedemptionRefused("invalid_payload")
    try:
        identify_hasher(password_hash)
    except ValueError as exc:
        raise RedemptionRefused("invalid_payload") from exc
    return password_hash


@transaction.atomic
def apply_redemption(redemption: Redemption, *, now: datetime | None = None) -> User:
    """Development turns a redemption production accepted into the account (ADR 0052 §9).

    Judged again **here**, against today's wording and today's published documents — the same
    discipline as the rights writes (`marceline/customer_writes.py`): production could only check
    what it was shown, and a declaration whose version has moved on in the meantime is refused
    rather than recorded as agreement to text nobody displayed.

    Ends in `complete()`, so the account appears exactly as it does when the development endpoint
    answers the last step: one transaction, `select_for_update`, `Membership.full_clean()` and the
    issue #92 guard. An invite already redeemed is **not an error** — the same person pressed twice,
    or an acknowledgement was lost — and the caller reports it as applied with no effect.
    """
    moment = now or timezone.now()
    invite = (
        CustomerInvite.objects.select_for_update().filter(token_hash=redemption.token_hash).first()
    )
    if invite is None or invite.revoked_at is not None or invite.expires_at <= moment:
        raise RedemptionRefused(REFUSED)
    if invite.redeemed_at is not None:
        raise AlreadyRedeemed
    if (
        redemption.declaration_version != CURRENT_BUSINESS_STATUS_DECLARATION.version
        or redemption.age_declaration_version != CURRENT_AGE_DECLARATION.version
    ):
        raise RedemptionRefused("declaration_version_outdated")
    if redemption.country.strip().upper() not in business_status_rules.EU_MEMBER_STATES:
        raise RedemptionRefused("country_required")
    try:
        published = onboarding.published_documents()
    except onboarding.DocumentsNotPublished as exc:
        raise RedemptionRefused("documents_not_published") from exc
    presented = {(row.document_id, row.version, row.sha256) for row in redemption.documents}
    if presented != {
        (document.document_id, document.version, document.sha256) for document in published
    }:
        raise RedemptionRefused("documents_outdated")

    invite.password_hash = _checked_password_hash(redemption.password_hash)
    invite.declaration_version = CURRENT_BUSINESS_STATUS_DECLARATION.version
    invite.declaration_text = CURRENT_BUSINESS_STATUS_DECLARATION.text()
    invite.declared_at = redemption.declared_at
    invite.age_declaration_version = CURRENT_AGE_DECLARATION.version
    invite.age_declaration_text = CURRENT_AGE_DECLARATION.text()
    invite.age_declared_at = redemption.age_declared_at
    invite.country = redemption.country.strip().upper()
    invite.save(
        update_fields=[
            "password_hash",
            "declaration_version",
            "declaration_text",
            "declared_at",
            "age_declaration_version",
            "age_declaration_text",
            "age_declared_at",
            "country",
        ]
    )
    if not invite.acceptances.exists():
        record_acceptances(invite, language=redemption.language, now=redemption.accepted_at)
    return complete(invite, now=moment, language=redemption.language)


def revoke(invite: CustomerInvite, *, reason: str = "", now: datetime | None = None) -> None:
    """Staff withdraw an invite. The reason is staff-only: the link keeps answering `REFUSED`."""
    if invite.revoked_at is not None:
        return
    invite.revoked_at = now or timezone.now()
    invite.revoked_reason = reason.strip()[:200]
    invite.save(update_fields=["revoked_at", "revoked_reason"])


def password_problems(password: str, email: str) -> list[str]:
    """Why Django's validators refuse this password for the address on an invite, as **codes**.

    The account does not exist yet, so the probe user carries the address the invite fixes, which
    is what `UserAttributeSimilarityValidator` compares against. The codes and the minimum length
    live in `accounts/passwords.py`, shared with the password reset (ADR 0054): one definition of
    what a refusal is called.
    """
    user_model = get_user_model()
    return passwords.problems(password, user_model(username=email, email=email))


def password_min_length() -> int:
    """What `MinimumLengthValidator` demands, so the screen can say the number."""
    return passwords.min_length()


def counts(*, now: datetime | None = None) -> dict[str, int]:
    """Open / redeemed / expired, for `make status` and the admin. Counts only, no addresses."""
    moment = now or timezone.now()
    rows = CustomerInvite.objects.all()
    return {
        "open": rows.filter(
            revoked_at__isnull=True, redeemed_at__isnull=True, expires_at__gt=moment
        ).count(),
        "redeemed": rows.filter(redeemed_at__isnull=False).count(),
        "expired": rows.filter(
            revoked_at__isnull=True, redeemed_at__isnull=True, expires_at__lte=moment
        ).count(),
        "revoked": rows.filter(revoked_at__isnull=False).count(),
    }
