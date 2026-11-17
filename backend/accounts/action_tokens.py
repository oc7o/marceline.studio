"""The pure half of the mailed account actions (ADR 0054, issue #149).

No model import, no database, no request: the token's shape, the two kinds' lifetimes, the one
refusal every failure shares, and whether this machine may run the flows at all.

Three rules live here because they must not be re-decided per caller — the same three that carry
onboarding by invite (ADR 0052), and deliberately the *same* helpers rather than a second pair:

1. **A token is never stored.** `new_token()` returns the secret once, into exactly one mail;
   `token_hash()` is what a row keeps. A lookup hashes what arrived and compares digests, which is
   also why an unknown token costs what a real one costs.
2. **One refusal.** `REFUSED` is the only thing any layer may say about a link that does not work —
   unknown, malformed, expired, already used, handed to the outbox, or the feature switched off. A
   second sentence would turn a mailed link into an oracle for "does this address have an account".
3. **No transport, no flow — on the machine that mails.** The flag alone is not enough on
   development: with no mailer configured a request would record a token nobody can ever receive. On
   production the flag *is* enough, because that machine never mails; it relays (issue #153).
   `available()` is the single answer to "may this machine carry the flow", `mails_here()` to "may
   this process send", and every route and every outbox handler asks the first one before anything
   else.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings

from accounts.onboarding import new_token, token_hash, token_well_formed

#: The one thing anybody says about a link that does not work. See rule 2.
REFUSED = "link_not_available"

#: What a request for a reset or a resend answers, whatever happened. It is deliberately not a
#: statement: "if this address has an account, a mail is on its way" is true for an address that has
#: none, and the screen says exactly that (ADR 0054 §2).
ACCEPTED = "mail_requested"

#: Django's local mail backends need no host: they *are* a configured transport (console prints,
#: locmem collects, filebased writes). Only the SMTP backend needs somewhere to connect to, and a
#: flow switched on without one would mint tokens nobody can receive.
LOCAL_MAIL_BACKENDS = frozenset(
    {
        f"django.core.mail.backends.{name}.EmailBackend"
        for name in ("console", "locmem", "filebased", "dummy")
    }
)

__all__ = [
    "ACCEPTED",
    "LOCAL_MAIL_BACKENDS",
    "REFUSED",
    "available",
    "flows_enabled",
    "mail_configured",
    "mails_here",
    "new_token",
    "reset_ttl",
    "token_hash",
    "token_well_formed",
    "verification_ttl",
]


def flows_enabled() -> bool:
    """`ACCOUNT_EMAIL_FLOWS_ENABLED`. Off means every route answers its neutral response."""
    return bool(getattr(settings, "ACCOUNT_EMAIL_FLOWS_ENABLED", False))


def mail_configured() -> bool:
    """Whether this process has somewhere to hand a mail to.

    Reads `MAILERS["default"]` (Django 6.1; the classic `EMAIL_*` settings are deprecated and
    `marceline/settings.py` does not use them). An SMTP backend without a host is **not**
    configured: the send would fail per mail and the customer would be told a mail is on its way.
    """
    mailer = (getattr(settings, "MAILERS", {}) or {}).get("default", {}) or {}
    backend = str(mailer.get("BACKEND", ""))
    if not backend:
        return False
    if backend in LOCAL_MAIL_BACKENDS:
        return True
    return bool((mailer.get("OPTIONS", {}) or {}).get("host", ""))


def available() -> bool:
    """Whether this machine can carry the flows at all. What every route asks first.

    **The predicate is per role, because the two machines do different halves of one flow**
    (issue #153, ADR 0054 amendment 2026-09-20):

    - on **development** it is the flag *and* a transport. This is the machine that mints a token
      and mails it, so without a mailer it would mint links nobody can receive — worse than a
      refusal;
    - on **production** it is the flag alone. That machine **never mails**: it takes the request or
      the redemption into the outbox and development does the rest, and it is refused a mail
      transport on purpose (`infra/prod/check_config.py`). Asking it for one made every mailed link
      answer the neutral 404 there with the flag on — the exact failure ADR 0054 §1 exists to
      avoid, because a creator who forgot their password is not on the tailnet.

    Whether the outbox can take the write is a **separate, temporary** question and stays where it
    was: the write paths answer 503 for it (`marceline/app/account_actions.py`), which a link that
    will never work must not be confused with.
    """
    if not flows_enabled():
        return False
    if settings.MARCELINE_ROLE == "prod":
        return True
    return mail_configured()


def mails_here() -> bool:
    """Whether **this** process may hand a customer mail to a transport (ADR 0054 §1).

    Development only, and it needs a transport. Production is refused by role, not by the absence of
    a mailer: `available()` no longer asks about one there, so something has to, and one function is
    the place rather than each caller's own `role != "prod"`.
    """
    return flows_enabled() and settings.MARCELINE_ROLE != "prod" and mail_configured()


def reset_ttl() -> timedelta:
    """How long a reset link works; at least a minute, so a zero is not an instant death."""
    minutes = max(int(getattr(settings, "ACCOUNT_PASSWORD_RESET_TTL_MINUTES", 60)), 1)
    return timedelta(minutes=minutes)


def verification_ttl() -> timedelta:
    """How long a verification link works; at least an hour for the same reason."""
    return timedelta(hours=max(int(getattr(settings, "ACCOUNT_VERIFICATION_TTL_HOURS", 48)), 1))
