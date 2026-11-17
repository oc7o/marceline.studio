"""Handing one of the two customer mails to a transport (ADR 0054, issue #149).

**Development only, and `_send` refuses by role** (issue #153). The public host has no mail
transport and is given none (ADR 0054 §1): a request there becomes an outbox item, and this module
runs where the outbox is applied. Nothing here decides *whether* a mail is due —
`accounts.account_actions` does — and nothing here knows what a link means; the role check is the
last wall, not the decision.

Three properties of every mail it sends:

- `Auto-Submitted: auto-generated` (RFC 3834), so a vacation responder or a ticket system does not
  answer it and a mailing list does not treat it as a post;
- a `Reply-To` that reaches a human (`ACCOUNT_MAIL_REPLY_TO`), because `noreply@` must not be a dead
  end for somebody who did not ask for the mail;
- **plain text only** — one `body`, no alternative part, no attachment, no image
  (`accounts/mail_templates.py` says why).

**It never raises.** A transport that is down must not fail the request a customer is waiting on, or
make an outbox item fail for ever; `send_*` returns False and the caller records that. And it never
logs the address or the link: a mail log that carries the reset URL is a credential store nobody
meant to build.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.core.mail import EmailMessage

from accounts import action_tokens, mail_templates

logger = logging.getLogger(__name__)


def recipient_of(user: AbstractBaseUser) -> str:
    """Where a mail to this account goes: the address on the row, or the username, which *is* one.

    Every customer account is created from an invited address (ADR 0052 §1) and keeps it as its
    username, so the two agree; the fallback is here for an account created by hand in the admin
    before this flow existed.
    """
    email = str(getattr(user, "email", "") or "").strip()
    return email or user.get_username()


def _headers() -> dict[str, str]:
    return {"Auto-Submitted": "auto-generated"}


def _reply_to() -> list[str]:
    address = str(getattr(settings, "ACCOUNT_MAIL_REPLY_TO", "") or "").strip()
    return [address] if address else []


def _send(*, to: str, text: mail_templates.MailText, link: str, **values: object) -> bool:
    """One mail, one transport call. True when the transport took it.

    Refuses outright on the public host (issue #153). `accounts.action_tokens.available()` is true
    there now — it relays the request rather than mailing it — so the role check that used to be
    implied by "no transport configured" is said out loud, here, at the one place a mail can leave
    the process.
    """
    if not to or settings.MARCELINE_ROLE == "prod":
        return False
    body = text.body.format(link=link, contact=_contact(), **values)
    try:
        # `using="default"` targets Django 6.1's MAILERS["default"] (marceline/settings.py) and
        # avoids every deprecated send_mail()/get_connection() code path, exactly as the safety
        # alerts do (`crawler/alerts.py`).
        sent = EmailMessage(
            subject=text.subject,
            body=body,
            from_email=settings.ACCOUNT_MAIL_FROM,
            to=[to],
            reply_to=_reply_to(),
            headers=_headers(),
        ).send(using="default")
    except Exception:
        logger.exception("account mail not sent (transport failed)")
        return False
    return bool(sent)


def _contact() -> str:
    """The address the mails name for a human answer. `ACCOUNT_MAIL_REPLY_TO`, or nothing."""
    return str(getattr(settings, "ACCOUNT_MAIL_REPLY_TO", "") or "").strip()


def send_password_reset(user: AbstractBaseUser, *, link: str, language: str) -> bool:
    """The reset mail. One link, its lifetime in minutes, and what to do if it was not them."""
    code = mail_templates.language_of(language)
    minutes = int(action_tokens.reset_ttl().total_seconds() // 60)
    return _send(
        to=recipient_of(user),
        text=mail_templates.PASSWORD_RESET[code],
        link=link,
        minutes=minutes,
    )


def send_verification(user: AbstractBaseUser, *, link: str, language: str) -> bool:
    """The verification mail. One link, its lifetime in hours, and that nothing is blocked by it."""
    code = mail_templates.language_of(language)
    hours = int(action_tokens.verification_ttl().total_seconds() // 3600)
    return _send(
        to=recipient_of(user),
        text=mail_templates.EMAIL_VERIFICATION[code],
        link=link,
        hours=hours,
    )
