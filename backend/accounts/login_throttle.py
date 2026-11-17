"""Rate limiting and lockout for session logins (ADR 0023).

Both login endpoints — the staff one (`/api/v1/auth/login`) and the customer one
(`/api/v1/app/auth/login`) — are on the public internet and answer the same question, so they share
one implementation instead of growing two sets of limits to reason about.

Two buckets, counted separately:

- **per account**, so guessing one customer's password is limited no matter where the attempts come
  from. The bucket is keyed by the *submitted* username, so an account that does not exist is
  locked out exactly like one that does — the lockout can never be used to enumerate accounts;
- **per IP**, so one host cannot spray one attempt each at a thousand usernames and stay under
  every account limit. An IPv6 address counts as its /64 (`ip_bucket`): one customer line is
  handed a whole /64, so counting single addresses would count nothing.

**Admission and counting are one step.** `begin()` runs *before* the password is verified: under a
per-bucket Postgres advisory lock it decides, and if the attempt may proceed it is recorded as a
failure right away, in the same transaction. A burst of parallel requests is therefore serialised
per bucket and cannot slip more attempts past the limit than the limit — a separate check followed
by a later write could. A correct password takes its own attempt back (`succeeded`).

A refused attempt costs no password hash, is not counted, and does not extend the lockout.

**One clock** (`_verdict`). A bucket is locked when its newest `limit` failures fall within one
counting window (`LOGIN_ATTEMPT_WINDOW_MINUTES`), and it stays locked until
`LOGIN_LOCKOUT_MINUTES` after the newest of them. `Retry-After` is exactly that moment, whatever the
two settings are relative to each other, so a client that waits as told is admitted, and hammering
never extends the lockout. The accepted cost of a per-account lockout is that somebody who knows a
username can lock its owner out for that long; it is short for exactly that reason, and
`manage.py unlock_login` lifts it immediately.

Everything here is keyed by digest (`accounts.LoginAttempt`), so nothing in this module ever stores
a username or an IP address. Rotating `SECRET_KEY` changes every key and therefore clears every
lockout — a deliberate, documented consequence, not a bug.
"""

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connection, transaction
from django.http import HttpRequest
from django.utils import timezone

from accounts.models import LoginAttempt

#: First key of the two-integer advisory lock, so login locks never collide with the single-key
#: locks other jobs take (scheduler, clustering, capture, discovery spend).
ADVISORY_LOCK_NAMESPACE = 0x10614
IPV6_BUCKET_PREFIX = 64


@dataclass(frozen=True)
class Refusal:
    """Why a login was refused before the password was even looked at."""

    scope: str
    retry_after_s: int


@dataclass(frozen=True)
class Attempt:
    """An admitted login attempt, already counted as a failure until `succeeded` says otherwise."""

    ids: tuple[int, ...]


def _window() -> timedelta:
    return timedelta(minutes=max(int(settings.LOGIN_ATTEMPT_WINDOW_MINUTES), 0))


def _lockout() -> timedelta:
    return timedelta(minutes=max(int(settings.LOGIN_LOCKOUT_MINUTES), 0))


def ip_bucket(value: str) -> str:
    """What the per-IP bucket counts: an IPv4 address, or the /64 an IPv6 address belongs to.

    IPv4-mapped IPv6 counts as the IPv4 address. Anything that is not an address is counted as the
    string it is, so a malformed `REMOTE_ADDR` still has a bucket.
    """
    text = value.strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{address}/{IPV6_BUCKET_PREFIX}", strict=False))
    return str(address)


def attempt_key(scope: str, value: str) -> str:
    """The digest a bucket is counted under. Never reversible, and never stored in the clear.

    Usernames are case-folded first: an attacker must not be able to start a fresh bucket by
    changing the capitalisation of a name Django would authenticate anyway. Addresses are reduced
    to their bucket (`ip_bucket`).
    """
    normalised = value.strip()
    if scope == LoginAttempt.Scope.ACCOUNT:
        normalised = normalised.casefold()
    elif scope == LoginAttempt.Scope.IP:
        normalised = ip_bucket(normalised)
    return hmac.new(
        settings.SECRET_KEY.encode(), f"{scope}:{normalised}".encode(), hashlib.sha256
    ).hexdigest()


def client_ip(request: HttpRequest) -> str:
    """The address the per-IP bucket is counted under.

    `REMOTE_ADDR` by default, because `X-Forwarded-For` is caller-supplied and trusting it blindly
    turns the per-IP limit into a header an attacker can rewrite per request. Behind a reverse
    proxy (Caddy, ADR 0005) every request arrives from the proxy instead, which would make the
    per-IP bucket global — so `LOGIN_TRUSTED_PROXIES` says how many proxies are in front of Django
    and the address is taken that many entries from the right of `X-Forwarded-For`, the only part
    of the header a client cannot forge. A header too short for the configured count falls back to
    `REMOTE_ADDR`: a shared bucket is a worse experience than a spoofable one is a hole.
    """
    trusted = max(int(settings.LOGIN_TRUSTED_PROXIES), 0)
    if trusted:
        forwarded = [
            part.strip()
            for part in request.headers.get("X-Forwarded-For", "").split(",")
            if part.strip()
        ]
        if len(forwarded) >= trusted:
            return forwarded[-trusted]
    return str(request.META.get("REMOTE_ADDR", "") or "")


def _buckets(username: str, ip: str) -> list[tuple[str, str, int]]:
    """(scope, value, limit) for each bucket this attempt counts against, account first."""
    return [
        (scope, value.strip(), limit)
        for scope, value, limit in (
            (LoginAttempt.Scope.ACCOUNT, username, int(settings.LOGIN_MAX_ACCOUNT_ATTEMPTS)),
            (LoginAttempt.Scope.IP, ip, int(settings.LOGIN_MAX_IP_ATTEMPTS)),
        )
        if value.strip()
    ]


def _verdict(scope: str, value: str, limit: int, now: datetime) -> Refusal | None:
    """One bucket: locked while its newest `limit` failures lie within one window and the newest
    is less than a lockout ago. The single definition `check`, `begin` and Retry-After share."""
    if limit <= 0:
        return None
    newest = list(
        LoginAttempt.objects.filter(
            key=attempt_key(scope, value), created_at__gt=now - _window() - _lockout()
        )
        .order_by("-created_at")
        .values_list("created_at", flat=True)[:limit]
    )
    if len(newest) < limit or newest[0] - newest[-1] >= _window():
        return None
    locked_until = newest[0] + _lockout()
    if now >= locked_until:
        return None
    return Refusal(scope=scope, retry_after_s=max(int((locked_until - now).total_seconds()), 1))


def check(username: str, ip: str, *, now: datetime | None = None) -> Refusal | None:
    """`None` when this attempt may proceed, a `Refusal` when it may not. Reads only.

    The endpoints use `begin`, which decides and counts atomically; this is the read-only verdict
    for tools and tests.
    """
    moment = now or timezone.now()
    for scope, value, limit in _buckets(username, ip):
        refusal = _verdict(scope, value, limit, moment)
        if refusal is not None:
            return refusal
    return None


def begin(username: str, ip: str, *, now: datetime | None = None) -> Attempt | Refusal:
    """Admit and count one login attempt atomically, or refuse it without counting it.

    Call before verifying the password; then `succeeded` on a correct one. A wrong password needs
    no further call: the attempt is already counted.
    """
    moment = now or timezone.now()
    buckets = _buckets(username, ip)
    with transaction.atomic():
        with connection.cursor() as cursor:
            # Sorted, so two requests locking the same pair of buckets never deadlock.
            for lock in sorted(
                {_lock_id(attempt_key(scope, value)) for scope, value, _ in buckets}
            ):
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)", [ADVISORY_LOCK_NAMESPACE, lock]
                )
        for scope, value, limit in buckets:
            refusal = _verdict(scope, value, limit, moment)
            if refusal is not None:
                return refusal
        rows = LoginAttempt.objects.bulk_create(
            LoginAttempt(scope=scope, key=attempt_key(scope, value), created_at=moment)
            for scope, value, _ in buckets
        )
    purge_expired(now=moment)
    return Attempt(ids=tuple(row.pk for row in rows if row.pk is not None))


def succeeded(attempt: Attempt, username: str) -> None:
    """The password was right: take this attempt back and clear the account's bucket.

    The IP bucket keeps its earlier failures (see `record_success`).
    """
    LoginAttempt.objects.filter(pk__in=attempt.ids).delete()
    record_success(username)


def record_failure(username: str, ip: str, *, now: datetime | None = None) -> None:
    """Count one failed password check against both buckets, and drop what has expired.

    Unconditional — for tools and tests. The endpoints count through `begin`.
    """
    moment = now or timezone.now()
    rows = [
        LoginAttempt(scope=scope, key=attempt_key(scope, value), created_at=moment)
        for scope, value, _ in _buckets(username, ip)
    ]
    if rows:
        LoginAttempt.objects.bulk_create(rows)
    purge_expired(now=moment)


def record_success(username: str) -> None:
    """Clear the account's bucket after a correct password.

    The IP bucket is deliberately left alone: an attacker who owns one valid account could
    otherwise reset their spray budget whenever they liked by signing into it.
    """
    if username.strip():
        LoginAttempt.objects.filter(
            key=attempt_key(LoginAttempt.Scope.ACCOUNT, username.strip())
        ).delete()


def unlock(username: str) -> int:
    """Lift an account lockout by hand (`manage.py unlock_login`). Returns rows removed."""
    if not username.strip():
        return 0
    deleted, _ = LoginAttempt.objects.filter(
        key=attempt_key(LoginAttempt.Scope.ACCOUNT, username.strip())
    ).delete()
    return int(deleted)


def unlock_ip(ip: str) -> int:
    """Lift a per-IP throttle by hand (for IPv6, the whole /64). Returns rows removed."""
    if not ip.strip():
        return 0
    deleted, _ = LoginAttempt.objects.filter(
        key=attempt_key(LoginAttempt.Scope.IP, ip.strip())
    ).delete()
    return int(deleted)


def purge_expired(*, now: datetime | None = None) -> int:
    """Delete attempts that can no longer refuse anything. Returns rows removed.

    A failure matters only among a bucket's newest ones within one window of each other, and only
    until a lockout after the newest: anything older than window plus lockout never can.
    """
    moment = now or timezone.now()
    deleted, _ = LoginAttempt.objects.filter(
        created_at__lt=moment - _window() - _lockout()
    ).delete()
    return int(deleted)


def _lock_id(key: str) -> int:
    """A signed 32-bit advisory lock id from a bucket digest."""
    return int(key[:8], 16) - 2**31
