"""Ending every session of one account (ADR 0054 §3).

A password reset must not leave a stolen session signed in — that is half of what a reset is *for*.
Django already does most of it: a session stores a hash of the password
(`AbstractBaseUser.get_session_auth_hash`), so changing the password invalidates every session on
the machine that holds them, at the next request. Two things that leaves undone, which this module
closes:

- **the delay.** Production holds the customer sessions and only learns the new password with the
  next `pull_accounts` (up to 15 minutes, ADR 0028). So production deletes the account's sessions
  **itself**, in the request that accepts the reset: the session store is production's own data and
  always has been (ADR 0028, "Accounts, sessions and other writes"), so this is a local write, not a
  customer write that needs the outbox;
- **saying it happened.** A count in a log line, so an operator reading a reset in the admin can see
  that sessions were ended and how many.

The session backend is the database one (`django.contrib.sessions.backends.db`), so the only way to
find one account's sessions is to decode the unexpired ones: there is no index on the user. That is
fine at this size (a few hundred rows), and the alternative — storing a user id beside each
session — would be a second place that has to be kept true.
"""

from __future__ import annotations

import logging
from datetime import datetime

from django.contrib.auth.models import AbstractBaseUser
from django.contrib.sessions.models import Session
from django.utils import timezone

logger = logging.getLogger(__name__)


def end_sessions(user: AbstractBaseUser, *, now: datetime | None = None) -> int:
    """Delete every unexpired session that belongs to this account. Returns how many.

    Never raises on a session it cannot decode (a rotated `SECRET_KEY` leaves those behind): such a
    session cannot authenticate anybody either, so it is skipped rather than reported.
    """
    moment = now or timezone.now()
    target = str(user.pk)
    doomed: list[str] = []
    for session in Session.objects.filter(expire_date__gte=moment).only("session_key"):
        try:
            data = session.get_decoded()
        except Exception:
            continue
        if str(data.get("_auth_user_id", "")) == target:
            doomed.append(session.session_key)
    if not doomed:
        return 0
    deleted, _ = Session.objects.filter(session_key__in=doomed).delete()
    logger.info("ended %s session(s) of account %s", deleted, user.pk)
    return int(deleted)
