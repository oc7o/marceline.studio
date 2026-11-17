"""The only writer of the account-control tables (ADR 0055).

`account_control.py` holds the rules, this holds the workflow, and **`rights/services.py` stays the
one notion of "verified"**: a challenge that staff confirm calls `services.verify_identity`, so the
notice gate's `identity_not_verified` clears through the field it always read
(`RightsClaimant.identity_status`). There is no second verified flag anywhere, deliberately.

What each step is, and who may take it:

| step | who | what it writes |
|---|---|---|
| `claim_account` | customer or staff | the claim (platform, handle, optional URL) and a code |
| `reissue_code` | staff | a new code on the same claim — **the previous one stops working** |
| `mark_placed` | customer | "I have put it up", nothing more |
| `verify` | staff only | the outcome, the method, and `services.verify_identity` |
| | | — `video_call` only where the platform runs no identity check of its own |
| `reject` | staff only | the outcome; identity is untouched |
| `revoke_verification` | staff only | takes it back, and `revoke_identity` with no other left |
| `expire_due` | system | codes nobody looked at in time |
| `expire_if_due` | system | the same, for the one row a write is about |

Three rules the code enforces rather than documents:

- **nothing here fetches anything.** The claimed profile URL is text for a human to open
  (`account_control.py`'s docstring, and `test_account_control.py`'s no-HTTP-import test);
- **no identity document and no date of birth is stored** — the `video_call` fallback
  records only *that* a check happened, by whom, when and by which method;
- **`staff_legacy` is never written to a challenge** (a database constraint): it is what the status
  reads for an organisation whose identity was recorded through ADR 0034's admin path instead.
"""

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from accounts.models import Organisation
from marceline.rights import account_control, services
from marceline.rights.models import (
    CHALLENGE_METHODS,
    SETTLED_VERIFICATION_STATES,
    AccountControlChallenge,
    AccountControlEvent,
    AccountControlEventKind,
    ActorKind,
    IdentityMethod,
    IdentityStatus,
    VerificationMethod,
    VerificationState,
)

#: How a challenge's method is recorded on the claimant (ADR 0034's older vocabulary). Placing a
#: code in a profile *is* the account-control token check; a video call is its own method.
IDENTITY_METHOD_OF: dict[str, str] = {
    VerificationMethod.BIO_CODE: IdentityMethod.ACCOUNT_CONTROL_TOKEN,
    VerificationMethod.DM_CODE: IdentityMethod.ACCOUNT_CONTROL_TOKEN,
    VerificationMethod.VIDEO_CALL: IdentityMethod.VIDEO_CALL,
}
#: Methods that need a live code in front of the staff member's eyes. A video call does not: it is
#: the fallback for doubt and for studios, and it establishes nothing through the code.
CODE_METHODS = frozenset({VerificationMethod.BIO_CODE, VerificationMethod.DM_CODE})


@dataclass(frozen=True)
class ClaimInput:
    """What a creator claims: a platform from the fixed list, their handle, optionally the URL."""

    platform: str
    handle: str
    profile_url: str = ""


def available() -> bool:
    """Whether this deployment runs the flow at all (`ACCOUNT_CONTROL_VERIFICATION_ENABLED`)."""
    return bool(settings.ACCOUNT_CONTROL_VERIFICATION_ENABLED)


def _require_available() -> None:
    if not available():
        raise services.RightsError("account_verification_unavailable")


def _log(
    challenge: AccountControlChallenge,
    kind: str,
    actor: services.Actor,
    *,
    method: str = "",
    note: str = "",
    detail: dict[str, object] | None = None,
) -> AccountControlEvent:
    return AccountControlEvent.objects.create(
        challenge=challenge,
        kind=kind,
        method=method,
        actor_kind=actor.kind,
        actor_id=actor.user_id,
        actor_label=actor.label,
        note=note.strip(),
        detail=detail or {},
    )


def challenges_for(organisation: Organisation) -> QuerySet[AccountControlChallenge]:
    """Every claim of this organisation, newest first. Scoped — never a second organisation's."""
    return AccountControlChallenge.objects.filter(organisation=organisation).order_by(
        "-created_at", "-pk"
    )


def display_state(challenge: AccountControlChallenge, *, now: datetime | None = None) -> str:
    """The state to **show**: an open challenge whose code ran out reads `expired`.

    Pure and read-only. Expiry is a fact about a timestamp, and a read may not write the row that
    records it (ADR 0028: the public cache answers reads from a bundle and writes nothing);
    `expire_due` settles it the next time a write or the admin passes by.
    """
    if challenge.state in SETTLED_VERIFICATION_STATES:
        return challenge.state
    if _code_expired(challenge, now or timezone.now()):
        return VerificationState.EXPIRED
    return challenge.state


def _code_expired(challenge: AccountControlChallenge, now: datetime) -> bool:
    return challenge.code_expires_at is not None and challenge.code_expires_at <= now


def _issue(challenge: AccountControlChallenge, now: datetime) -> None:
    """Put a fresh code on this claim. Overwriting is the point: the old one stops working.

    A platform that runs no identity check of its own gets **no code at all**: one placed there
    could never establish who the creator is (ADR 0055, operator review 2026-09-19), so issuing one
    would ask somebody to do something pointless and then refuse them for it.
    """
    coded = account_control.checks_identity(challenge.platform)
    challenge.code = account_control.new_code() if coded else ""
    challenge.code_issued_at = now if coded else None
    challenge.code_expires_at = account_control.expires_at(now) if coded else None
    challenge.issue_count += 1
    challenge.state = VerificationState.PENDING
    challenge.placed_at = None
    challenge.placed_by = None
    challenge.method = ""
    challenge.decided_at = None
    challenge.decided_by = None
    challenge.save()


def _issue_kind(actor: services.Actor) -> str:
    """A staff member *issues* a code; a creator asking for their own is *shown* it.

    The distinction is worth a separate event kind: it is the difference between "we prepared this"
    and "the code reached the creator", and it is the only moment we can honestly record the
    second. A read never writes one (see `display_state`).
    """
    return (
        AccountControlEventKind.SHOWN
        if actor.kind == ActorKind.CUSTOMER
        else AccountControlEventKind.ISSUED
    )


@transaction.atomic
def claim_account(
    organisation: Organisation, claim: ClaimInput, actor: services.Actor
) -> AccountControlChallenge:
    """Claim one platform account and get a code for it.

    A claim that already stands **verified** is returned untouched — there is nothing to prove
    twice. Any other state (open, rejected, expired, revoked) gets a fresh code, which invalidates
    whatever code was there.
    """
    _require_available()
    try:
        handle = account_control.normalise_handle(claim.handle)
        profile_url = account_control.checked_profile_url(claim.platform, claim.profile_url)
    except account_control.InvalidClaimError as error:
        raise services.RightsError(error.code) from error
    now = timezone.now()
    challenge, created = AccountControlChallenge.objects.select_for_update().get_or_create(
        organisation=organisation,
        platform=claim.platform,
        handle=handle,
        defaults={"profile_url": profile_url},
    )
    if challenge.state == VerificationState.VERIFIED:
        return challenge
    challenge.profile_url = profile_url or challenge.profile_url
    _issue(challenge, now)
    _log(
        challenge,
        _issue_kind(actor),
        actor,
        detail={"platform": claim.platform, "issue": challenge.issue_count, "new": created},
    )
    return challenge


@transaction.atomic
def reissue_code(
    challenge: AccountControlChallenge, actor: services.Actor
) -> AccountControlChallenge:
    """A new code for a claim staff are looking at. The previous code stops working at once."""
    _require_available()
    if actor.kind != ActorKind.STAFF:
        raise services.RightsError("staff_only")
    locked = AccountControlChallenge.objects.select_for_update().get(pk=challenge.pk)
    if locked.state == VerificationState.VERIFIED:
        raise services.RightsError("already_verified")
    _issue(locked, timezone.now())
    _log(locked, AccountControlEventKind.ISSUED, actor, detail={"issue": locked.issue_count})
    return locked


def expire_if_due(challenge: AccountControlChallenge, *, now: datetime | None = None) -> bool:
    """Settle this challenge if its code has run out. **Its own transaction**, deliberately.

    A refusal that rolls back is a refusal nobody recorded: `mark_placed` and `verify` raise after
    finding the code expired, so settling it inside their transaction would undo the very row that
    says why — found by `test_an_expired_code_cannot_be_placed_or_verified`.
    """
    at = now or timezone.now()
    with transaction.atomic():
        locked = AccountControlChallenge.objects.select_for_update().get(pk=challenge.pk)
        if locked.state in SETTLED_VERIFICATION_STATES or not _code_expired(locked, at):
            return False
        _settle(locked, VerificationState.EXPIRED)
        _log(locked, AccountControlEventKind.EXPIRED, services.SYSTEM)
    challenge.refresh_from_db()
    return True


def mark_placed(
    challenge: AccountControlChallenge, actor: services.Actor
) -> AccountControlChallenge:
    """The creator says the code is up. It is a statement, not a check: nobody is verified by it."""
    _require_available()
    if not account_control.checks_identity(challenge.platform):
        # There is no code on such a claim (`_issue`), so there is nothing to have placed.
        raise services.RightsError("video_call_required")
    if expire_if_due(challenge):
        raise services.RightsError("code_expired")
    with transaction.atomic():
        locked = AccountControlChallenge.objects.select_for_update().get(pk=challenge.pk)
        if locked.state in SETTLED_VERIFICATION_STATES:
            raise services.RightsError("not_open")
        now = timezone.now()
        locked.state = VerificationState.PLACED
        locked.placed_at = now
        locked.placed_by = actor.account
        locked.save(update_fields=["state", "placed_at", "placed_by", "updated_at"])
        _log(locked, AccountControlEventKind.PLACED, actor)
    return locked


def _settle(challenge: AccountControlChallenge, state: str) -> None:
    """Close a challenge and drop its code: a settled row has no live code to compare."""
    challenge.state = state
    challenge.code = ""
    challenge.save(update_fields=["state", "code", "updated_at"])


def verify(
    challenge: AccountControlChallenge,
    *,
    method: str,
    note: str = "",
    actor: services.Actor,
) -> AccountControlChallenge:
    """Staff record that they saw it, by hand, and identity is verified for the organisation.

    `bio_code` / `dm_code` need the code still to be live — a staff member comparing an expired code
    is comparing something the creator was told to remove. `video_call` does not: it is the fallback
    for doubt and for studios, and **only the fact of the check is recorded** (date, staff user,
    method), never an image, a number or a birth date.
    """
    _require_available()
    if actor.kind != ActorKind.STAFF:
        raise services.RightsError("staff_only")
    if method not in CHALLENGE_METHODS:
        raise services.RightsError("unknown_verification_method")
    if method in CODE_METHODS and not account_control.checks_identity(challenge.platform):
        # Nothing checked who this person is before they published there, so a code in that page's
        # bio says only that somebody controls the page. The video call is the only way here.
        raise services.RightsError("video_call_required")
    if method in CODE_METHODS and expire_if_due(challenge):
        raise services.RightsError("code_expired")
    with transaction.atomic():
        locked = AccountControlChallenge.objects.select_for_update().get(pk=challenge.pk)
        if locked.state in SETTLED_VERIFICATION_STATES:
            raise services.RightsError("not_open")
        locked.method = method
        locked.decided_at = timezone.now()
        locked.decided_by = actor.account
        locked.note = note.strip()
        locked.state = VerificationState.VERIFIED
        locked.code = ""
        locked.save()
        _log(locked, AccountControlEventKind.VERIFIED, actor, method=method, note=note)
        # The one notion of "verified" (ADR 0034). The evidence note is built from the record, so
        # no identity data can travel into it by hand.
        services.verify_identity(
            locked.organisation,
            method=IDENTITY_METHOD_OF[method],
            evidence_note=(
                f"account control, {method}: {locked.platform}/{locked.handle}"
                f"{'; ' + note.strip() if note.strip() else ''}"
            ),
            actor=actor,
        )
    return locked


@transaction.atomic
def reject(
    challenge: AccountControlChallenge, *, note: str = "", actor: services.Actor
) -> AccountControlChallenge:
    """Staff did not see the code. Identity is untouched; a new code is a new attempt."""
    _require_available()
    if actor.kind != ActorKind.STAFF:
        raise services.RightsError("staff_only")
    locked = AccountControlChallenge.objects.select_for_update().get(pk=challenge.pk)
    if locked.state in SETTLED_VERIFICATION_STATES:
        raise services.RightsError("not_open")
    locked.note = note.strip()
    locked.decided_at = timezone.now()
    locked.decided_by = actor.account
    locked.save(update_fields=["note", "decided_at", "decided_by", "updated_at"])
    _settle(locked, VerificationState.REJECTED)
    _log(locked, AccountControlEventKind.REJECTED, actor, note=note)
    return locked


@transaction.atomic
def revoke_verification(
    challenge: AccountControlChallenge, *, reason: str, actor: services.Actor
) -> AccountControlChallenge:
    """Take a verification back, with a reason.

    The organisation's identity verification is revoked **only when no other verified account is
    left**: two accounts proved and one withdrawn does not unmake the other one.
    """
    _require_available()
    if actor.kind != ActorKind.STAFF:
        raise services.RightsError("staff_only")
    if not reason.strip():
        raise services.RightsError("reason_required")
    locked = AccountControlChallenge.objects.select_for_update().get(pk=challenge.pk)
    if locked.state != VerificationState.VERIFIED:
        raise services.RightsError("not_verified")
    now = timezone.now()
    locked.decided_at = now
    locked.decided_by = actor.account
    locked.save(update_fields=["decided_at", "decided_by", "updated_at"])
    _settle(locked, VerificationState.REVOKED)
    _log(locked, AccountControlEventKind.REVOKED, actor, note=reason)
    remaining = (
        AccountControlChallenge.objects.filter(
            organisation=locked.organisation, state=VerificationState.VERIFIED
        )
        .exclude(pk=locked.pk)
        .exists()
    )
    if not remaining:
        services.revoke_identity(locked.organisation, reason=reason, actor=actor)
    return locked


def expire_due(*, now: datetime | None = None) -> list[AccountControlChallenge]:
    """Settle every open challenge whose code has run out. Idempotent; writes one event each."""
    at = now or timezone.now()
    expired = []
    for challenge in AccountControlChallenge.objects.filter(
        state__in=(VerificationState.PENDING, VerificationState.PLACED),
        code_expires_at__lte=at,
    ).order_by("pk"):
        with transaction.atomic():
            locked = AccountControlChallenge.objects.select_for_update().get(pk=challenge.pk)
            if locked.state in SETTLED_VERIFICATION_STATES:
                continue
            _settle(locked, VerificationState.EXPIRED)
            _log(locked, AccountControlEventKind.EXPIRED, services.SYSTEM)
            expired.append(locked)
    return expired


def method_of(organisation: Organisation) -> str:
    """How this organisation's identity was established, as a code — or "" while it is not.

    A verified challenge answers with its own method. An organisation verified through ADR 0034's
    admin path (no challenge, or a challenge nobody decided) reads `staff_legacy`: the older path is
    kept, and this is how it is attributed.
    """
    claimant = services.claimant_for(organisation)
    if claimant.identity_status != IdentityStatus.VERIFIED:
        return ""
    verified = (
        AccountControlChallenge.objects.filter(
            organisation=organisation, state=VerificationState.VERIFIED
        )
        .order_by("-decided_at", "-pk")
        .first()
    )
    return verified.method if verified is not None else VerificationMethod.STAFF_LEGACY
