"""The two mails this product sends to a customer, verbatim (ADR 0054, issue #149).

They are the **only** mail any code path sends to a customer, and everything about them is
deliberate:

- **plain text, no HTML alternative.** Nothing to render, nothing to load, nothing that can carry a
  tracking pixel — and a mail client's plain-text view is where a suspicious reader looks anyway;
- **one link, and it is on our own customer app's host.** No unsubscribe link (this is not
  marketing), no help centre, no social account, no image, no logo, no web font, no redirect through
  a click tracker. A second link would train creators that our mails carry several, which is exactly
  what the phishing that targets them looks like;
- **German is the operative language** (the agreement is German, `accounts/declarations.py`); the
  English is the reading aid a customer gets when their app is in English. „du" in German, like the
  app and the website (operator decision 2026-09-19);
- **no name, no organisation, no address in the body.** The mail arrived in the reader's own inbox;
  telling them their own address back adds nothing and would put it in every forwarded copy;
- **it says what to do if it was not them**: the reset mail's last line. A mail that only offers an
  action teaches nobody what an unexpected one means;
- **no duration promises and no legal claims** — the words are checked by the same rules as the
  app's (`frontend/src/i18n/wording.test.ts` for the UI; `accounts/tests/test_account_mail.py` for
  these).

A wording change is a change **here**, in one place, with its test. There is no template directory
and no `render_to_string`: these are two short texts, and a template engine would put the sentences
somewhere a reviewer does not look.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Bumped when a text changes, so a mail that went out can be identified by what it said. Not stored
#: on a row today: nothing needs to prove the wording of a transactional mail, and a version in the
#: code is enough to answer "what did the mail say on that date" from the history.
VERSION = "2026-09-19"

OPERATIVE_LANGUAGE = "de"


@dataclass(frozen=True)
class MailText:
    subject: str
    body: str


def _lines(*lines: str) -> str:
    return "\n".join(lines) + "\n"


PASSWORD_RESET: dict[str, MailText] = {
    "de": MailText(
        subject="Marceline: neues Passwort setzen",
        body=_lines(
            "Du hast für dein Marceline-Konto ein neues Passwort angefordert.",
            "",
            "Öffne dazu diesen Link:",
            "{link}",
            "",
            "Der Link gilt {minutes} Minuten und funktioniert nur einmal.",
            "",
            "Wenn du das nicht angefordert hast, ist nichts passiert: dein Passwort bleibt, wie es"
            " ist. Du musst nichts tun. Wenn dich solche Mails öfter erreichen, schreib uns an"
            " {contact}.",
        ),
    ),
    "en": MailText(
        subject="Marceline: set a new password",
        body=_lines(
            "You asked for a new password for your Marceline account.",
            "",
            "Open this link to set one:",
            "{link}",
            "",
            "The link works for {minutes} minutes, and only once.",
            "",
            "If this was not you, nothing has happened: your password is unchanged and there is"
            " nothing you need to do. Write to {contact} if mails like this keep arriving.",
        ),
    ),
}

EMAIL_VERIFICATION: dict[str, MailText] = {
    "de": MailText(
        subject="Marceline: E-Mail-Adresse bestätigen",
        body=_lines(
            "Bitte bestätige, dass diese Adresse zu deinem Marceline-Konto gehört.",
            "",
            "Öffne dazu diesen Link:",
            "{link}",
            "",
            "Der Link gilt {hours} Stunden und funktioniert nur einmal. Du kannst dein Konto auch"
            " ohne die Bestätigung benutzen — wir brauchen sie, damit wir dich später zuverlässig"
            " erreichen können.",
            "",
            "Wenn du kein Konto bei uns hast, ignoriere diese Mail oder schreib uns an {contact}.",
        ),
    ),
    "en": MailText(
        subject="Marceline: confirm your email address",
        body=_lines(
            "Please confirm that this address belongs to your Marceline account.",
            "",
            "Open this link to confirm it:",
            "{link}",
            "",
            "The link works for {hours} hours, and only once. You can use your account without"
            " confirming — we need it so that we can reach you reliably later on.",
            "",
            "If you have no account with us, ignore this mail or write to {contact}.",
        ),
    ),
}


def language_of(requested: str) -> str:
    """The language a mail is written in: the one asked for if we have it, otherwise German.

    German rather than English, because it is the operative language of the agreement and the
    audience the beta is for. An unknown or empty value is not an error — a browser sent it.
    """
    code = (requested or "").strip().lower()[:2]
    return code if code in PASSWORD_RESET else OPERATIVE_LANGUAGE
