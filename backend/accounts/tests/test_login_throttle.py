"""The counting rules behind the login lockout (ADR 0023).

The endpoint tests live in `marceline/tests/test_app_auth.py`; these are the parts that are easier
to state than to observe through HTTP — that nothing stores a username or an address, that an
expired attempt stops counting, and that a forwarded header is not believed unless it was
configured to be.
"""

import threading
import time
from datetime import datetime, timedelta

import pytest
from django.db import connection
from django.test import RequestFactory, override_settings
from django.utils import timezone

from accounts import login_throttle
from accounts.models import LoginAttempt

USERNAME = "someone@example.test"
IP = "203.0.113.7"

LIMITS = override_settings(
    LOGIN_MAX_ACCOUNT_ATTEMPTS=3,
    LOGIN_MAX_IP_ATTEMPTS=5,
    LOGIN_ATTEMPT_WINDOW_MINUTES=15,
    LOGIN_LOCKOUT_MINUTES=15,
    LOGIN_TRUSTED_PROXIES=0,
)


def fail(times: int, username: str = USERNAME, ip: str = IP) -> None:
    for _ in range(times):
        login_throttle.record_failure(username, ip)


@LIMITS
def test_an_attempt_stores_a_digest_not_a_username_or_an_address(db: None) -> None:
    """The table must be useless to whoever reads it: no names, no addresses, no plaintext."""
    fail(1)

    stored = {row.key for row in LoginAttempt.objects.all()}

    assert len(stored) == 2
    assert USERNAME not in stored
    assert IP not in stored
    assert all(len(key) == 64 and key.isalnum() for key in stored)


@LIMITS
def test_capitalisation_does_not_start_a_fresh_bucket(db: None) -> None:
    fail(2)
    login_throttle.record_failure(USERNAME.upper(), IP)

    assert login_throttle.check(USERNAME, IP) is not None


@LIMITS
def test_the_account_is_refused_only_at_the_limit(db: None) -> None:
    fail(2)
    assert login_throttle.check(USERNAME, IP) is None

    fail(1)
    refusal = login_throttle.check(USERNAME, IP)

    assert refusal is not None
    assert refusal.scope == LoginAttempt.Scope.ACCOUNT
    # Fifteen minutes from the last failure, give or take the second this test took.
    assert 14 * 60 <= refusal.retry_after_s <= 15 * 60


@LIMITS
def test_the_ip_bucket_counts_attempts_spread_over_many_accounts(db: None) -> None:
    """One attempt each at five usernames stays under every account limit — and is still a spray."""
    for index in range(5):
        login_throttle.record_failure(f"victim-{index}@example.test", IP)

    refusal = login_throttle.check("someone-else@example.test", IP)

    assert refusal is not None
    assert refusal.scope == LoginAttempt.Scope.IP


@LIMITS
def test_a_correct_password_clears_the_account_but_not_the_ip(db: None) -> None:
    """Otherwise one account an attacker owns would reset their whole spray budget."""
    for index in range(5):
        login_throttle.record_failure(f"victim-{index}@example.test", IP)
    login_throttle.record_failure(USERNAME, IP)

    login_throttle.record_success(USERNAME)

    assert login_throttle.check(USERNAME, "198.51.100.9") is None
    assert login_throttle.check(USERNAME, IP) is not None


@LIMITS
def test_attempts_outside_the_window_stop_counting_and_are_purged(db: None) -> None:
    fail(3)
    assert login_throttle.check(USERNAME, IP) is not None

    LoginAttempt.objects.update(created_at=timezone.now() - timedelta(minutes=16))

    assert login_throttle.check(USERNAME, IP) is None
    assert login_throttle.purge_expired(now=timezone.now() + timedelta(minutes=30)) == 6
    assert LoginAttempt.objects.count() == 0


@LIMITS
def test_unlock_lifts_the_lockout_immediately(db: None) -> None:
    fail(3)

    assert login_throttle.unlock(USERNAME) == 3
    assert login_throttle.check(USERNAME, IP) is None


@override_settings(LOGIN_TRUSTED_PROXIES=0)
def test_a_forwarded_header_is_ignored_unless_a_proxy_is_configured() -> None:
    request = RequestFactory().post(
        "/api/v1/app/auth/login", HTTP_X_FORWARDED_FOR="9.9.9.9", REMOTE_ADDR="10.0.0.5"
    )

    assert login_throttle.client_ip(request) == "10.0.0.5"


@override_settings(LOGIN_TRUSTED_PROXIES=1)
def test_one_proxy_in_front_means_the_last_forwarded_entry() -> None:
    """The right-most entry is the one the proxy wrote; everything left of it is claimed by the
    client and would let anyone pick their own rate-limit bucket."""
    request = RequestFactory().post(
        "/api/v1/app/auth/login",
        HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.7",
        REMOTE_ADDR="10.0.0.5",
    )

    assert login_throttle.client_ip(request) == "203.0.113.7"


@override_settings(LOGIN_TRUSTED_PROXIES=2)
def test_a_header_too_short_to_trust_falls_back_to_the_socket() -> None:
    request = RequestFactory().post(
        "/api/v1/app/auth/login", HTTP_X_FORWARDED_FOR="1.2.3.4", REMOTE_ADDR="10.0.0.5"
    )

    assert login_throttle.client_ip(request) == "10.0.0.5"


# --- atomic claims -----------------------------------------------------------------------------


@LIMITS
def test_an_admitted_attempt_is_counted_before_the_password_is_checked(db: None) -> None:
    attempt = login_throttle.begin(USERNAME, IP)

    assert isinstance(attempt, login_throttle.Attempt)
    assert LoginAttempt.objects.count() == 2  # account and IP, already


@LIMITS
def test_a_success_takes_its_own_attempt_back_and_clears_the_account(db: None) -> None:
    fail(2)
    attempt = login_throttle.begin(USERNAME, IP)
    assert isinstance(attempt, login_throttle.Attempt)

    login_throttle.succeeded(attempt, USERNAME)

    assert not LoginAttempt.objects.filter(scope=LoginAttempt.Scope.ACCOUNT).exists()
    # The IP keeps the two real failures, not the attempt that turned out to be right.
    assert LoginAttempt.objects.filter(scope=LoginAttempt.Scope.IP).count() == 2


@LIMITS
def test_a_refused_attempt_is_not_counted(db: None) -> None:
    fail(3)

    refusal = login_throttle.begin(USERNAME, IP)

    assert isinstance(refusal, login_throttle.Refusal)
    assert LoginAttempt.objects.filter(scope=LoginAttempt.Scope.ACCOUNT).count() == 3


@override_settings(
    LOGIN_MAX_ACCOUNT_ATTEMPTS=3,
    LOGIN_MAX_IP_ATTEMPTS=1000,
    LOGIN_ATTEMPT_WINDOW_MINUTES=15,
    LOGIN_LOCKOUT_MINUTES=15,
)
@pytest.mark.django_db(transaction=True)
def test_a_parallel_burst_cannot_exceed_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twelve simultaneous guesses at one account: three get to try a password, not twelve."""
    verdict = login_throttle._verdict

    def slow_verdict(scope: str, value: str, limit: int, now: datetime) -> object:
        result = verdict(scope, value, limit, now)
        time.sleep(0.02)  # widen the gap between counting and recording, as a busy server would
        return result

    monkeypatch.setattr(login_throttle, "_verdict", slow_verdict)
    start = threading.Barrier(12)
    admitted: list[bool] = []

    def guess(index: int) -> None:
        try:
            start.wait()
            result = login_throttle.begin(USERNAME, f"198.51.100.{index}")
            admitted.append(isinstance(result, login_throttle.Attempt))
        finally:
            connection.close()

    threads = [threading.Thread(target=guess, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admitted) == 12
    assert admitted.count(True) == 3
    assert LoginAttempt.objects.filter(scope=LoginAttempt.Scope.ACCOUNT).count() == 3


# --- IPv6 and the lockout clock ----------------------------------------------------------------


@LIMITS
def test_ipv6_addresses_share_a_bucket_per_64(db: None) -> None:
    """One customer line gets a whole /64; counting single addresses would count nothing."""
    for suffix in ("1", "2", "dead:beef", "ffff:ffff:fffe", "5"):
        login_throttle.record_failure(f"victim-{suffix}@example.test", f"2001:db8:1:2::{suffix}")

    refusal = login_throttle.check("someone-else@example.test", "2001:db8:1:2:abcd::9")

    assert refusal is not None and refusal.scope == LoginAttempt.Scope.IP
    assert login_throttle.check("someone-else@example.test", "2001:db8:1:3::1") is None
    assert login_throttle.unlock_ip("2001:db8:1:2::77") == 5


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        (" 2001:DB8::1 ", "2001:db8::/64"),
        ("not-an-address", "not-an-address"),
    ],
)
def test_ip_bucket_value(value: str, expected: str) -> None:
    assert login_throttle.ip_bucket(value) == expected


@pytest.mark.parametrize(("window", "lockout"), [(15, 15), (15, 5), (5, 30)])
def test_retry_after_is_exactly_when_the_next_attempt_is_admitted(
    db: None, window: int, lockout: int
) -> None:
    """One source of truth: however window and lockout relate, Retry-After is the moment `check`
    admits again — never earlier (a second refusal) and never later (a promise not kept)."""
    now = timezone.now()
    with override_settings(
        LOGIN_MAX_ACCOUNT_ATTEMPTS=3,
        LOGIN_MAX_IP_ATTEMPTS=100,
        LOGIN_ATTEMPT_WINDOW_MINUTES=window,
        LOGIN_LOCKOUT_MINUTES=lockout,
    ):
        for minutes in (2, 1, 0):
            login_throttle.record_failure(USERNAME, IP, now=now - timedelta(minutes=minutes))

        refusal = login_throttle.check(USERNAME, IP, now=now)
        assert refusal is not None
        assert refusal.retry_after_s == lockout * 60

        until = now + timedelta(seconds=refusal.retry_after_s)
        assert login_throttle.check(USERNAME, IP, now=until - timedelta(seconds=1)) is not None
        assert login_throttle.check(USERNAME, IP, now=until) is None


@LIMITS
def test_failures_spread_wider_than_the_window_never_lock(db: None) -> None:
    now = timezone.now()
    for minutes in (40, 20, 0):
        login_throttle.record_failure(USERNAME, IP, now=now - timedelta(minutes=minutes))

    assert login_throttle.check(USERNAME, IP, now=now) is None
