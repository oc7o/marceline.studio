"""The two customer mails say what they should and nothing else (ADR 0054, issue #149).

These are the only mails this product sends to a customer, and every rule about them is here rather
than in a reviewer's memory: plain text, one link, no tracking, no image, a reply address that
reaches a human, `Auto-Submitted`, and no claim or promise the app's own wording rules would ban.
"""

import re
from collections.abc import Iterator

import pytest
from django.contrib.auth.models import User
from django.core import mail as django_mail
from django.test import override_settings
from pytest_django.fixtures import Settings

from accounts import mail, mail_templates
from accounts.models import Membership, Organisation

pytestmark = pytest.mark.django_db

LINK = "https://dash.example.invalid/app/reset-password/abcdefghijklmnopqrstuvwxyz012345"

#: What a transactional mail of ours must never contain. The app's own bans are
#: `frontend/src/i18n/wording.test.ts`; these are the ones that matter in an inbox.
FORBIDDEN = (
    # No tracking of any kind: no pixel, no redirect, no per-recipient query string.
    re.compile(r"<img", re.I),
    re.compile(r"utm_", re.I),
    re.compile(r"click\.|track\.|open\.gif", re.I),
    # No HTML at all.
    re.compile(r"</?(html|body|table|a\b)", re.I),
    # No legal verdict and no enforcement promise (as in the app).
    re.compile(r"\bleak|\bpirat|rechtswidrig|urheberrechtsverletz", re.I),
    re.compile(r"\bgarant|\bguarantee", re.I),
    # No unsubscribe: this is not marketing, and an unsubscribe link in a reset mail is a phishing
    # shape. No second link of any kind, which the link count below also pins.
    re.compile(r"unsubscribe|abmelden", re.I),
)


@pytest.fixture(autouse=True)
def enabled(settings: Settings) -> None:
    settings.ACCOUNT_EMAIL_FLOWS_ENABLED = True


@pytest.fixture(autouse=True)
def empty_outbox() -> Iterator[None]:
    django_mail.outbox.clear()
    yield
    django_mail.outbox.clear()


@pytest.fixture
def customer() -> User:
    organisation = Organisation.objects.create(name="A Studio")
    user = User.objects.create_user(
        "creator@example.invalid", email="creator@example.invalid", password="x"
    )
    Membership.objects.create(organisation=organisation, user=user, role=Membership.Role.OWNER)
    return user


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("kind", ["reset", "verification"])
def test_each_mail_is_plain_text_with_exactly_one_link(
    customer: User, language: str, kind: str
) -> None:
    send = mail.send_password_reset if kind == "reset" else mail.send_verification

    assert send(customer, link=LINK, language=language) is True

    [sent] = django_mail.outbox
    # `EmailMessage` (not `EmailMultiAlternatives`) cannot carry an HTML part at all, which is the
    # point: the type is the guarantee, not a runtime check somebody could relax.
    assert not hasattr(sent, "alternatives"), "an HTML part would mean a different message class"
    assert sent.attachments == []
    assert sent.content_subtype == "plain"
    assert sent.body.count("https://") == 1, sent.body
    assert LINK in sent.body
    for pattern in FORBIDDEN:
        assert not pattern.search(str(sent.body)), f"{pattern.pattern} in {sent.body!r}"
        assert not pattern.search(str(sent.subject)), pattern.pattern


@pytest.mark.parametrize("kind", ["reset", "verification"])
def test_each_mail_names_no_address_and_no_organisation(customer: User, kind: str) -> None:
    """It arrived in the reader's own inbox; repeating their address only travels with a forward."""
    send = mail.send_password_reset if kind == "reset" else mail.send_verification

    send(customer, link=LINK, language="de")

    [sent] = django_mail.outbox
    assert customer.get_username() not in sent.body
    assert "A Studio" not in sent.body
    assert sent.to == [customer.get_username()]


@pytest.mark.parametrize("kind", ["reset", "verification"])
def test_each_mail_carries_the_headers_an_automated_mail_needs(customer: User, kind: str) -> None:
    send = mail.send_password_reset if kind == "reset" else mail.send_verification

    send(customer, link=LINK, language="en")

    [sent] = django_mail.outbox
    assert sent.extra_headers["Auto-Submitted"] == "auto-generated"
    assert sent.reply_to == ["contact@marceline.studio"]
    assert sent.from_email == "noreply@marceline.studio"


def test_the_reset_mail_says_what_to_do_if_it_was_not_them(customer: User) -> None:
    mail.send_password_reset(customer, link=LINK, language="de")
    [de] = django_mail.outbox
    assert "nicht angefordert" in de.body
    assert "60 Minuten" in de.body, "the lifetime the setting really has"

    django_mail.outbox.clear()
    mail.send_password_reset(customer, link=LINK, language="en")
    [en] = django_mail.outbox
    assert "not you" in en.body
    assert "60 minutes" in en.body


def test_the_verification_mail_says_nothing_is_blocked(customer: User) -> None:
    """ADR 0054 §3: an unconfirmed address blocks nothing, so the mail must not imply it does."""
    mail.send_verification(customer, link=LINK, language="de")
    [de] = django_mail.outbox
    assert "ohne die Bestätigung benutzen" in de.body
    assert "48 Stunden" in de.body

    django_mail.outbox.clear()
    mail.send_verification(customer, link=LINK, language="en")
    [en] = django_mail.outbox
    assert "without confirming" in en.body


def test_german_says_du(customer: User) -> None:
    """The customer surfaces say „du" (operator decision 2026-09-19); a mail is one of them."""
    formal = re.compile(r"\b(Sie|Ihre[nmrs]?|Ihr|Ihnen)\b")

    mail.send_password_reset(customer, link=LINK, language="de")
    mail.send_verification(customer, link=LINK, language="de")

    for sent in django_mail.outbox:
        assert not formal.search(str(sent.body)), sent.body
        assert not formal.search(str(sent.subject)), sent.subject


def test_an_unknown_language_is_written_in_german() -> None:
    """German is the operative language of the agreement, so it is the fallback (not English)."""
    assert mail_templates.language_of("") == "de"
    assert mail_templates.language_of("fr-FR") == "de"
    assert mail_templates.language_of("EN-gb") == "en"


def test_a_transport_failure_is_reported_not_raised(customer: User) -> None:
    """A mail server that is down must not fail a customer's request or poison an outbox item."""
    with override_settings(
        MAILERS={
            "default": {"BACKEND": "accounts.tests.test_account_mail.BrokenBackend", "OPTIONS": {}}
        }
    ):
        assert mail.send_password_reset(customer, link=LINK, language="de") is False


class BrokenBackend:
    """A mailer that refuses, as an unreachable SMTP server does."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def send_messages(self, messages: object) -> int:
        raise OSError("no route to host")


def test_every_text_has_both_languages_and_one_placeholder_set() -> None:
    """A missing language would fall back to German silently; a stray placeholder would raise."""
    for texts in (mail_templates.PASSWORD_RESET, mail_templates.EMAIL_VERIFICATION):
        assert set(texts) == {"de", "en"}
        for text in texts.values():
            assert "{link}" in text.body
            assert "{contact}" in text.body
