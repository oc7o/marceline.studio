"""Issuing, mailing and redeeming the two account links (ADR 0054, issue #149).

The half that writes. `accounts/action_tokens.py` holds the token's shape, the lifetimes and the one
refusal; `accounts/mail.py` hands a text to a transport; `marceline/app/account_actions.py` turns
this into HTTP.

Four things are load-bearing and easy to lose in a refactor:

- **`lookup()` is the only way in**, as it is for an invite. It hashes what arrived and returns
  the row or `None`, and every reason for `None` — unknown, malformed, wrong kind, expired, used,
  handed to the outbox, feature off, no transport — is the same `None`. No caller may ask which.
- **A request never says whether the address has an account.** `request_password_reset` returns
  nothing a caller could branch on: it is `None` whether it mailed somebody or found nobody, and the
  endpoint answers the same `ACCEPTED` either way (ADR 0054 §2).
- **The token names the user, not the address.** A reset sets the password of the row's own user, so
  an address that changed in between cannot move a credential; and the lookup never touches the
  address at all.
- **Mail goes out on development only.** `request_*` are called there — by the endpoint on
  development's own surface, or by the outbox handler applying a request the public host accepted
  (ADR 0054 §1). Production issues no token and sends no mail; it has no transport.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import identify_hasher, is_password_usable
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from accounts import action_tokens, mail, sessions
from accounts.models import AccountActionToken, EmailVerification, Membership

logger = logging.getLogger(__name__)

Kind = AccountActionToken.Kind

#: How long a verification mail waits before a resend sends another one. A creator who presses the
#: button twice gets one mail, and a bored one cannot use us to post to somebody's inbox.
RESEND_INTERVAL = timedelta(minutes=10)


class ActionRefused(Exception):
    """Everything a caller may say about a link that does not work: `REFUSED`, and nothing else."""

    def __init__(self) -> None:
        super().__init__(action_tokens.REFUSED)


class PayloadRefused(Exception):
    """A write from the outbox development will not apply.

    `code` is staff-facing and is never a sentence.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- the links ----------------------------------------------------------------------------------
#
# `ONBOARDING_INVITE_BASE_URL` is the customer app's origin. Its name is historical (it arrived
# with the invite links of ADR 0052) and it is deliberately reused rather than joined by a second
# setting for the same value: two variables naming one origin are two chances to send a creator to
# a host that does not serve them.


def link_base() -> str:
    return str(getattr(settings, "ONBOARDING_INVITE_BASE_URL", "") or "").rstrip("/")


def reset_link(token: str) -> str:
    """Where a reset mail sends the reader. The Next route, under the app's `basePath`."""
    return f"{link_base()}/app/reset-password/{token}"


def verification_link(token: str) -> str:
    return f"{link_base()}/app/verify-email/{token}"


# --- issuing ------------------------------------------------------------------------------------


def _ttl(kind: str) -> timedelta:
    if kind == Kind.PASSWORD_RESET:
        return action_tokens.reset_ttl()
    return action_tokens.verification_ttl()


def issue(user: User, kind: str, *, now: datetime | None = None) -> tuple[AccountActionToken, str]:
    """Create a token for this account and return it with its secret. **Returned once, here.**

    Every earlier unused token of the same kind for the same account is marked used in the same
    transaction: asking again must not leave two working links in two inboxes.
    """
    moment = now or timezone.now()
    token = action_tokens.new_token()
    with transaction.atomic():
        AccountActionToken.objects.filter(
            user=user, kind=kind, used_at__isnull=True, handed_over_at__isnull=True
        ).update(used_at=moment)
        row = AccountActionToken.objects.create(
            kind=kind,
            user=user,
            token_hash=action_tokens.token_hash(token),
            created_at=moment,
            expires_at=moment + _ttl(kind),
        )
    # Housekeeping where it costs nothing: issuing is rare, and a token that expired yesterday is a
    # digest and two timestamps nobody will ever read. The same pattern as `login_throttle.begin`,
    # which purges its own buckets, and it keeps this feature out of the scheduler entirely.
    purge_expired(now=moment)
    return row, token


def customer_for(email: str) -> User | None:
    """The active customer account this address belongs to, or `None`.

    A **customer**: a member of an organisation. A staff reviewer has no membership and therefore no
    mailed reset — their password is the operator's business (`manage.py changepassword`, ADR 0023),
    and a mailed link into a staff account would be a way in that ADR 0028 keeps off the internet.

    Case-insensitive, because people type their address with whatever capitalisation their keyboard
    offered; the oldest row wins if two accounts differ only in case.
    """
    user_model = get_user_model()
    address = (email or "").strip()
    if not address:
        return None
    user = (
        user_model.objects.filter(username__iexact=address, is_active=True).order_by("pk").first()
    )
    if user is None or not Membership.objects.filter(user=user).exists():
        return None
    return user


def request_password_reset(email: str, *, language: str = "", now: datetime | None = None) -> None:
    """Mail a reset link to this address **if** it has a customer account (ADR 0054 §2).

    Returns nothing, on purpose: there is no value a caller could turn into "that address exists".
    An unknown address, a staff account, an inactive one and a transport failure all end here
    silently, and the endpoint answers the same sentence in every case.

    **Only ever runs on development.** `mails_here()` rather than `available()` since issue #153:
    `available()` is true on production now (it relays), and this function is the half that mints a
    token and hands a mail to a transport — which the public host must never do.
    """
    if not action_tokens.mails_here():
        return
    user = customer_for(email)
    if user is None:
        return
    moment = now or timezone.now()
    row, token = issue(user, Kind.PASSWORD_RESET, now=moment)
    if mail.send_password_reset(user, link=reset_link(token), language=language):
        AccountActionToken.objects.filter(pk=row.pk).update(mailed_at=moment)
    else:
        logger.error("password reset mail not sent for account %s", user.pk)


def verification_state(user: User) -> EmailVerification:
    row, _ = EmailVerification.objects.get_or_create(user=user)
    return row


def verified(user: User) -> bool:
    """Whether this account's address has been confirmed. False for an account that never had a row.

    Never a gate: nothing in the beta refuses a signed-in customer over this (ADR 0054 §3). It is a
    fact `GET /app/me` reports, so the app can ask, and a fact a later feature may rely on.
    """
    row = EmailVerification.objects.filter(user=user).first()
    return row is not None and row.verified_at is not None


def request_verification(
    user: User, *, language: str = "", now: datetime | None = None, force: bool = False
) -> None:
    """Mail a verification link to this account's address, unless one went out very recently.

    Called when an invite is redeemed (the address is then unconfirmed by definition) and from the
    customer's own "send it again". `force` is for staff tooling, never for a route; the interval
    is what keeps a button nobody can see the state of from becoming a way to post to an inbox.

    **Only ever runs on development**, for the reason `request_password_reset` gives (issue #153).
    """
    if not action_tokens.mails_here():
        return
    moment = now or timezone.now()
    state = verification_state(user)
    if state.verified_at is not None:
        return
    if (
        not force
        and state.last_sent_at is not None
        and moment - state.last_sent_at < RESEND_INTERVAL
    ):
        return
    row, token = issue(user, Kind.EMAIL_VERIFICATION, now=moment)
    if mail.send_verification(user, link=verification_link(token), language=language):
        AccountActionToken.objects.filter(pk=row.pk).update(mailed_at=moment)
        EmailVerification.objects.filter(pk=state.pk).update(last_sent_at=moment)
    else:
        logger.error("verification mail not sent for account %s", user.pk)


# --- redeeming ----------------------------------------------------------------------------------


def lookup(token: str, kind: str, *, now: datetime | None = None) -> AccountActionToken | None:
    """The token row this secret opens, or `None` — for every reason there is, indistinguishably.

    One indexed query on the digest, and the shape check runs first so a malformed token costs a
    regex instead of a query. The kind is part of the question: a verification link is not a reset
    link, and presenting one as the other is refused like an unknown token.
    """
    if not action_tokens.available() or not action_tokens.token_well_formed(token):
        return None
    moment = now or timezone.now()
    row = (
        AccountActionToken.objects.select_related("user")
        .filter(token_hash=action_tokens.token_hash(token), kind=kind)
        .first()
    )
    if row is None or not usable(row, now=moment):
        return None
    return row


def usable(row: AccountActionToken, *, now: datetime | None = None) -> bool:
    """Not used, not handed to the outbox, not expired, and the account is still active."""
    moment = now or timezone.now()
    return (
        row.used_at is None
        and row.handed_over_at is None
        and row.expires_at > moment
        and row.user.is_active
    )


def lock_for_handover(
    row: AccountActionToken, *, now: datetime | None = None
) -> AccountActionToken:
    """The row, locked, when the outbox may take its write — or `ActionRefused` (production).

    Called inside the transaction that enqueues, so two submissions produce **one** item: the second
    finds `handed_over_at` set and is refused with the same `REFUSED` as every other failure.
    """
    moment = now or timezone.now()
    locked = AccountActionToken.objects.select_for_update().select_related("user").get(pk=row.pk)
    if not usable(locked, now=moment):
        raise ActionRefused
    return locked


def mark_handed_over(
    row: AccountActionToken, *, submission_id: str, now: datetime | None = None
) -> None:
    """The write is in the outbox: this link is finished, whatever happens next (production)."""
    row.handed_over_at = now or timezone.now()
    row.submission_id = submission_id[:64]
    row.save(update_fields=["handed_over_at", "submission_id"])


def checked_password_hash(password_hash: str) -> str:
    """The hash, when Django recognises it as one of *its* hashers' — or `PayloadRefused`.

    `User.password` is set from this string as it arrives (the plain password never leaves the host
    that received it, ADR 0054 §1), so an unrecognised value would create an account nobody can sign
    in to, and a `!`-prefixed one an unusable password a later reset could take over. The same check
    as the invite redemption's (`accounts/invites.py`).
    """
    if not password_hash or not is_password_usable(password_hash):
        raise PayloadRefused("invalid_payload")
    try:
        identify_hasher(password_hash)
    except ValueError as exc:
        raise PayloadRefused("invalid_payload") from exc
    return password_hash


@transaction.atomic
def apply_password_reset(
    token_hash: str, password_hash: str, *, now: datetime | None = None
) -> User:
    """Development sets the new password and ends every session of that account (ADR 0054 §3).

    The token is named by its digest — the secret stays in the reader's mail — and is marked used in
    the same transaction, so a replayed outbox item finds nothing to do and settles with no effect.
    """
    moment = now or timezone.now()
    row = (
        AccountActionToken.objects.select_for_update()
        .select_related("user")
        .filter(token_hash=token_hash, kind=Kind.PASSWORD_RESET)
        .first()
    )
    if row is None or row.used_at is not None or row.expires_at <= moment or not row.user.is_active:
        raise PayloadRefused(action_tokens.REFUSED)
    user = row.user
    user.password = checked_password_hash(password_hash)
    user.save(update_fields=["password"])
    AccountActionToken.objects.filter(pk=row.pk).update(used_at=moment)
    # Every other unused reset of this account goes with it: one reset, one new password.
    AccountActionToken.objects.filter(
        user=user, kind=Kind.PASSWORD_RESET, used_at__isnull=True
    ).update(used_at=moment)
    sessions.end_sessions(user, now=moment)
    logger.info("password reset applied for account %s", user.pk)
    return user


@transaction.atomic
def apply_verification(token_hash: str, *, now: datetime | None = None) -> User:
    """Development records that this address was confirmed. Idempotent: a second one is a no-op."""
    moment = now or timezone.now()
    row = (
        AccountActionToken.objects.select_for_update()
        .select_related("user")
        .filter(token_hash=token_hash, kind=Kind.EMAIL_VERIFICATION)
        .first()
    )
    if row is None or row.expires_at <= moment or not row.user.is_active:
        raise PayloadRefused(action_tokens.REFUSED)
    state = verification_state(row.user)
    if state.verified_at is None:
        EmailVerification.objects.filter(pk=state.pk).update(verified_at=moment)
    AccountActionToken.objects.filter(pk=row.pk).update(used_at=moment)
    logger.info("address verified for account %s", row.user.pk)
    return row.user


# --- housekeeping -------------------------------------------------------------------------------


def purge_expired(*, now: datetime | None = None) -> int:
    """Delete tokens that expired more than a day ago. Returns how many.

    A used or expired token is a digest and two timestamps, so keeping it buys nothing: what an
    operator wants to know about a reset is in the log line and in the outbox item.
    """
    moment = now or timezone.now()
    deleted, _ = AccountActionToken.objects.filter(
        expires_at__lt=moment - timedelta(days=1)
    ).delete()
    return int(deleted)


def open_tokens(*, now: datetime | None = None) -> list[AccountActionToken]:
    """Every token a link could still open, for the signed accounts document (ADR 0054 §1)."""
    moment = now or timezone.now()
    return list(
        AccountActionToken.objects.filter(
            used_at__isnull=True, handed_over_at__isnull=True, expires_at__gt=moment
        ).order_by("pk")
    )
