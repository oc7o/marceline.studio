"""The two mailed account links, as a service (ADR 0054, issue #149).

The HTTP side is `marceline/tests/test_app_account_actions.py`; this is what the flows guarantee
whoever calls them: one refusal, single use, a short life, a mail that goes to the right inbox and
says nothing to anybody else, and a reset that ends every session.

Mail never leaves the process: Django's test runner swaps every mailer to `locmem`, and
`django.core.mail.outbox` is what these tests read.
"""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core import mail as django_mail
from django.test import override_settings
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts import account_actions, action_tokens
from accounts.models import AccountActionToken, EmailVerification, Membership, Organisation

pytestmark = pytest.mark.django_db

PASSWORD = "a-long-enough-passphrase"
Kind = AccountActionToken.Kind


@pytest.fixture(autouse=True)
def fast_password_hashing() -> Iterator[None]:
    with override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]):
        yield


@pytest.fixture(autouse=True)
def enabled(settings: Settings) -> None:
    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = True
    settings.ONBOARDING_INVITE_BASE_URL = "https://dash.example.invalid"


@pytest.fixture(autouse=True)
def empty_outbox() -> Iterator[None]:
    django_mail.outbox.clear()
    yield
    django_mail.outbox.clear()


def make_customer(address: str = "creator@example.invalid") -> User:
    organisation = Organisation.objects.create(name="A Studio")
    user = User.objects.create_user(address, email=address, password=PASSWORD)
    Membership.objects.create(organisation=organisation, user=user, role=Membership.Role.OWNER)
    return user


# --- the switch ---------------------------------------------------------------------------------


def test_the_flows_are_off_by_default(settings: Settings) -> None:
    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = False
    user = make_customer()

    account_actions.request_password_reset(user.get_username())
    account_actions.request_verification(user)

    assert django_mail.outbox == []
    assert not AccountActionToken.objects.exists()


def test_a_flow_without_a_transport_sends_nothing(settings: Settings) -> None:
    """The flag is not enough: a token nobody can receive is worse than a refusal (ADR 0054 §1)."""
    settings.MAILERS = {
        "default": {"BACKEND": "django.core.mail.backends.smtp.EmailBackend", "OPTIONS": {}}
    }
    user = make_customer()

    assert action_tokens.mail_configured() is False
    assert action_tokens.available() is False
    account_actions.request_password_reset(user.get_username())
    assert not AccountActionToken.objects.exists()


# --- asking for a reset -------------------------------------------------------------------------


def test_a_reset_mails_one_link_to_the_account(settings: Settings) -> None:
    user = make_customer()

    account_actions.request_password_reset(user.get_username(), language="de")

    row = AccountActionToken.objects.get()
    assert (row.kind, row.user_id) == (Kind.PASSWORD_RESET, user.pk)
    assert row.mailed_at is not None
    [sent] = django_mail.outbox
    assert sent.to == [user.get_username()]
    assert sent.from_email == "noreply@marceline.studio"
    assert sent.extra_headers["Auto-Submitted"] == "auto-generated"
    assert sent.reply_to == ["contact@marceline.studio"]
    # The link carries the token; the row keeps only its digest.
    links = [word for word in sent.body.split() if word.startswith("https://")]
    assert len(links) == 1, sent.body
    token = links[0].rsplit("/", 1)[-1]
    assert action_tokens.token_hash(token) == row.token_hash
    assert token not in str(AccountActionToken.objects.values())


@pytest.mark.parametrize(
    "who",
    ["unknown", "staff", "inactive", "no_membership"],
)
def test_nobody_else_gets_a_reset(who: str) -> None:
    """A staff account, an inactive one and an address with no account: no token, no mail.

    The *endpoint* answers the same 202 in every case (`test_app_account_actions.py`); what must not
    happen is a mailed link into an account this flow is not for.
    """
    if who == "staff":
        User.objects.create_user("reviewer@example.invalid", password=PASSWORD, is_staff=True)
        address = "reviewer@example.invalid"
    elif who == "inactive":
        user = make_customer()
        user.is_active = False
        user.save(update_fields=["is_active"])
        address = user.get_username()
    elif who == "no_membership":
        User.objects.create_user("stray@example.invalid", password=PASSWORD)
        address = "stray@example.invalid"
    else:
        address = "nobody@example.invalid"

    account_actions.request_password_reset(address)

    assert django_mail.outbox == []
    assert not AccountActionToken.objects.exists()


def test_the_address_is_matched_whatever_its_capitalisation() -> None:
    user = make_customer("Creator@Example.invalid")

    account_actions.request_password_reset("creator@example.INVALID")

    assert AccountActionToken.objects.get().user_id == user.pk


def test_asking_twice_leaves_one_working_link() -> None:
    """Two links in two mails is a second credential nobody took back (ADR 0054)."""
    user = make_customer()
    account_actions.request_password_reset(user.get_username())
    first = AccountActionToken.objects.get()

    account_actions.request_password_reset(user.get_username())

    assert AccountActionToken.objects.filter(used_at__isnull=True).count() == 1
    assert AccountActionToken.objects.get(pk=first.pk).used_at is not None


# --- the one refusal ----------------------------------------------------------------------------


def test_lookup_refuses_every_reason_the_same_way(settings: Settings) -> None:
    user = make_customer()
    row, token = account_actions.issue(user, Kind.PASSWORD_RESET)

    assert account_actions.lookup(token, Kind.PASSWORD_RESET) is not None
    # A verification link is not a reset link.
    assert account_actions.lookup(token, Kind.EMAIL_VERIFICATION) is None
    assert account_actions.lookup("not a token", Kind.PASSWORD_RESET) is None
    assert account_actions.lookup(action_tokens.new_token(), Kind.PASSWORD_RESET) is None

    later = timezone.now() + action_tokens.reset_ttl() + timedelta(seconds=1)
    assert account_actions.lookup(token, Kind.PASSWORD_RESET, now=later) is None

    account_actions.mark_handed_over(row, submission_id="sub_" + "0" * 32)
    assert account_actions.lookup(token, Kind.PASSWORD_RESET) is None

    AccountActionToken.objects.filter(pk=row.pk).update(handed_over_at=None, used_at=timezone.now())
    assert account_actions.lookup(token, Kind.PASSWORD_RESET) is None

    AccountActionToken.objects.filter(pk=row.pk).update(used_at=None)
    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = False
    assert account_actions.lookup(token, Kind.PASSWORD_RESET) is None


def test_a_reset_link_dies_with_the_account() -> None:
    user = make_customer()
    _, token = account_actions.issue(user, Kind.PASSWORD_RESET)
    User.objects.filter(pk=user.pk).update(is_active=False)

    assert account_actions.lookup(token, Kind.PASSWORD_RESET) is None


# --- setting the new password -------------------------------------------------------------------


def test_the_reset_sets_the_password_and_ends_every_session(client_session: str) -> None:
    from django.contrib.sessions.models import Session

    user = User.objects.get()
    _, token = account_actions.issue(user, Kind.PASSWORD_RESET)
    row = account_actions.lookup(token, Kind.PASSWORD_RESET)
    assert row is not None
    from django.contrib.auth.hashers import make_password

    account_actions.apply_password_reset(row.token_hash, make_password("another-long-passphrase"))

    user.refresh_from_db()
    assert user.check_password("another-long-passphrase")
    assert not Session.objects.filter(session_key=client_session).exists()
    assert AccountActionToken.objects.get().used_at is not None


def test_a_replayed_reset_is_refused_rather_than_applied_twice() -> None:
    from django.contrib.auth.hashers import make_password

    user = make_customer()
    _, token = account_actions.issue(user, Kind.PASSWORD_RESET)
    digest = action_tokens.token_hash(token)
    account_actions.apply_password_reset(digest, make_password("another-long-passphrase"))

    with pytest.raises(account_actions.PayloadRefused) as refused:
        account_actions.apply_password_reset(digest, make_password("a-third-passphrase"))

    assert refused.value.code == action_tokens.REFUSED
    user.refresh_from_db()
    assert user.check_password("another-long-passphrase")


def test_a_hash_django_does_not_recognise_is_refused() -> None:
    user = make_customer()
    _, token = account_actions.issue(user, Kind.PASSWORD_RESET)

    with pytest.raises(account_actions.PayloadRefused):
        account_actions.apply_password_reset(action_tokens.token_hash(token), "not-a-django-hash")

    user.refresh_from_db()
    assert user.check_password(PASSWORD), "the old password still works"


# --- verification -------------------------------------------------------------------------------


def test_a_verification_mail_goes_out_once_per_interval() -> None:
    user = make_customer()

    account_actions.request_verification(user, language="en")
    account_actions.request_verification(user, language="en")

    assert len(django_mail.outbox) == 1
    state = EmailVerification.objects.get(user=user)
    assert state.last_sent_at is not None and state.verified_at is None

    later = timezone.now() + account_actions.RESEND_INTERVAL + timedelta(seconds=1)
    account_actions.request_verification(user, language="en", now=later)
    assert len(django_mail.outbox) == 2


def test_confirming_the_address_is_idempotent() -> None:
    user = make_customer()
    account_actions.request_verification(user)
    [sent] = django_mail.outbox
    link = next(word for word in sent.body.split() if word.startswith("https://"))
    token = link.rsplit("/", 1)[-1]

    account_actions.apply_verification(action_tokens.token_hash(token))
    first = EmailVerification.objects.get(user=user).verified_at
    account_actions.apply_verification(action_tokens.token_hash(token))

    assert first is not None
    assert EmailVerification.objects.get(user=user).verified_at == first
    assert account_actions.verified(user) is True


def test_a_verified_address_is_not_mailed_again() -> None:
    user = make_customer()
    EmailVerification.objects.create(user=user, verified_at=timezone.now())

    account_actions.request_verification(user)

    assert django_mail.outbox == []


# --- housekeeping -------------------------------------------------------------------------------


def test_expired_tokens_are_purged_after_a_day() -> None:
    user = make_customer()
    row, _ = account_actions.issue(user, Kind.PASSWORD_RESET)
    AccountActionToken.objects.filter(pk=row.pk).update(
        expires_at=timezone.now() - timedelta(days=2)
    )

    assert account_actions.purge_expired() == 1
    assert not AccountActionToken.objects.exists()


def test_open_tokens_are_the_ones_a_link_could_still_open() -> None:
    user = make_customer()
    open_row, _ = account_actions.issue(user, Kind.PASSWORD_RESET)
    used, _ = account_actions.issue(user, Kind.EMAIL_VERIFICATION)
    AccountActionToken.objects.filter(pk=used.pk).update(used_at=timezone.now())

    assert [row.pk for row in account_actions.open_tokens()] == [open_row.pk]


@pytest.fixture
def client_session() -> str:
    """A signed-in session of one customer, as production would hold it. Returns its key."""
    from django.test import Client

    user = make_customer()
    client = Client()
    assert client.login(username=user.get_username(), password=PASSWORD)
    return str(client.cookies["sessionid"].value)
