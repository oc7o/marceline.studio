"""Why Django's validators refuse a password, as **codes** (ADR 0023, ADR 0052, ADR 0054).

One definition, three callers: the invite's password step, a password reset, and any screen that
wants to say the minimum length. Codes, not Django's English sentences: a customer payload carries
machine-readable values and the bilingual UI owns the words (`backend/marceline/CLAUDE.md`).
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

#: The codes Django's own validators raise. A validator without one is reported as
#: `password_invalid`, so a screen always has something to say.
KNOWN_CODES = (
    "password_too_short",
    "password_too_common",
    "password_entirely_numeric",
    "password_too_similar",
)


def problems(password: str, user: User) -> list[str]:
    """Django's verdict on this password for this account, as codes, or an empty list.

    `user` is passed so `UserAttributeSimilarityValidator` can do its work — it compares the
    password with the username and the address. That is why both hosts must judge a password with
    the same account in hand: with only a digest of the address they would disagree (ADR 0052
    amendment 2026-09-19), and a customer would be told on one host what the other refuses.
    """
    try:
        validate_password(password, user=user)
    except ValidationError as refusal:
        return [str(error.code or "password_invalid") for error in refusal.error_list]
    return []


def min_length() -> int:
    """What `MinimumLengthValidator` is configured to demand, so a screen can say the number.

    Read from `AUTH_PASSWORD_VALIDATORS` rather than repeated anywhere: one definition, and a change
    to the setting reaches the sentence the customer reads.
    """
    for validator in getattr(settings, "AUTH_PASSWORD_VALIDATORS", []):
        if str(validator.get("NAME", "")).endswith("MinimumLengthValidator"):
            return int(validator.get("OPTIONS", {}).get("min_length", 8))
    return 8
