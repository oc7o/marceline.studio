"""The do-not-contact record: normalising an identifier, keying it, and asking about it.

Someone we approached — or might approach — says "no, do not write to me again". That "no" is an
objection to direct marketing (Art. 21(2)-(3) GDPR): it is effective without any balancing, and it
has to be honoured for good. Honouring it needs memory, and memory of a person who told us to go
away is exactly the data we should not be keeping. So we keep the least that works:

**a keyed hash of the normalised identifier, its kind, the date, the channel, who recorded it.**

Nothing here can be read back into an address, a name or a handle: the identifier is normalised,
HMAC-ed with a server-side secret and thrown away. What the record answers is one closed question —
"is this identifier on the list?" — and it can only answer it for an identifier somebody already
holds. The shape is `normalised contact identifier / do-not-contact / date / scope`, on its own
narrow purpose, never keeping any research about the person.

**This is not index suppression.** ADR 0038 / `docs/suppression.md` answers a different sentence
("stop indexing me") with a different marker on a `Creator` row. Neither implies the other: someone
may want to be left alone by post and stay in the index, or the reverse, or both. See
`docs/do-not-contact.md`.

Normalisation is deliberately **conservative**. Two spellings of the same identifier should land on
the same hash; two different people must never. Where those pull apart — a phone number written
nationally and internationally, a handle without its platform — the rule keeps them apart and the
staff screen says to record both spellings, because a missed block is the failure that matters.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Iterable, Sequence
from hashlib import sha256

EMAIL = "email"
POSTAL = "postal"
ORGANISATION = "organisation"
HANDLE = "handle"
PHONE = "phone"

#: The identifier kinds a "no" can arrive on, with what staff type in.
KINDS: tuple[tuple[str, str], ...] = (
    (EMAIL, "Email address"),
    (POSTAL, "Postal address (the whole address, as written)"),
    (ORGANISATION, "Company or studio name plus its city"),
    (HANDLE, "Social handle, as `platform:handle` or a profile URL"),
    (PHONE, "Telephone number, exactly as it was given"),
)

#: How the "no" reached us. Recorded because Art. 5(2) GDPR asks us to be able to show it did.
CHANNELS: tuple[tuple[str, str], ...] = (
    ("letter", "Letter or postcard"),
    ("email", "Email"),
    ("phone", "Telephone"),
    ("in_person", "In person (trade fair, meeting)"),
    ("platform_message", "Message on a platform"),
    ("third_party", "Through an agency, a studio or a lawyer"),
    ("other", "Other — see the note"),
)

#: Letters that mean the same thing in German and are typed both ways.
_FOLDED = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "å": "a", "æ": "ae", "ø": "oe"}


class MissingSecret(RuntimeError):
    """`DO_NOT_CONTACT_HMAC_KEY` is not set.

    Hashing with an empty key would produce a digest anybody can recompute, which is the one thing
    the keying is for. Every path refuses instead — recording and checking alike, because a check
    that cannot see the records would answer "allowed" for everyone.
    """


class UnusableIdentifier(ValueError):
    """What was typed cannot be normalised into an identifier of that kind."""


def _fold(value: str) -> str:
    return "".join(_FOLDED.get(character, character) for character in value.casefold())


def _words(value: str) -> list[str]:
    """Lower-case words of `value`, umlauts folded, everything but letters and digits dropped."""
    return re.sub(r"[^a-z0-9]+", " ", _fold(value)).split()


def _street(word: str) -> str:
    """`musterstr` -> `musterstrasse`: the one German abbreviation typed both ways every time."""
    if word == "str":
        return "strasse"
    if word.endswith("str") and len(word) > 3:
        return f"{word[:-3]}strasse"
    return word


def normalise(kind: str, value: str) -> str:
    """The comparable form of one identifier. Pure, and the only place spellings are folded.

    Raises `UnusableIdentifier` when the input cannot be an identifier of that kind. The result is
    never stored or printed — it exists to be hashed.
    """
    raw = value.strip()
    if not raw:
        raise UnusableIdentifier("empty")
    if kind == EMAIL:
        local, separator, domain = raw.rpartition("@")
        if not separator or not local.strip() or not domain.strip():
            raise UnusableIdentifier("not an email address (no local part or no domain)")
        # Case only. No dot- or plus-folding: that is one provider's convention, and applying it
        # everywhere would put two different mailboxes on one hash.
        return f"{local.strip().casefold()}@{domain.strip().casefold().rstrip('.')}"
    if kind == POSTAL:
        words = [_street(word) for word in _words(raw)]
        if not words:
            raise UnusableIdentifier("no letters or digits in the address")
        return " ".join(words)
    if kind == ORGANISATION:
        # A conjunction distinguishes nobody ("Muster und Partner" is "Muster Partner"), a legal
        # form does ("GmbH" and "GmbH & Co. KG" are two companies), so only the first is dropped.
        words = [word for word in _words(raw) if word not in {"und", "and"}]
        if not words:
            raise UnusableIdentifier("no letters or digits in the name")
        return " ".join(words)
    if kind == HANDLE:
        return _handle(raw)
    if kind == PHONE:
        return _phone(raw)
    raise UnusableIdentifier(f"unknown identifier kind {kind!r}")


def _handle(raw: str) -> str:
    """`https://www.instagram.com/Name/` and `Instagram:@name` both become `instagram/name`.

    The platform is part of the identifier on purpose: the same handle on two networks is usually
    two people, and blocking a stranger by accident is its own harm. A bare handle is refused.
    """
    text = raw.strip()
    for prefix in ("https://", "http://"):
        text = text.removeprefix(prefix)
    if "/" in text:
        host, _, rest = text.partition("/")
        segments = [segment for segment in rest.split("?")[0].split("#")[0].split("/") if segment]
        if not segments:
            raise UnusableIdentifier("a profile URL needs the handle in its path")
        platform, handle = host, segments[-1]
    elif ":" in text:
        platform, _, handle = text.partition(":")
    else:
        raise UnusableIdentifier(
            "a handle needs its platform: type `platform:handle` or the profile URL"
        )
    platform = platform.strip().casefold().removeprefix("www.")
    platform = platform.partition(".")[0]
    handle = handle.strip().casefold().lstrip("@")
    if not platform or not handle:
        raise UnusableIdentifier("a handle needs a platform and a name")
    return f"{platform}/{handle}"


def _phone(raw: str) -> str:
    """Digits, with `00`/`+` as the one international prefix. No country is ever guessed.

    `+49 30 1234` and `0049 (0)30 1234` land on the same hash; a number written nationally
    (`030 1234`) does not, and cannot without assuming where the caller was. Record both spellings
    if both are known — `docs/do-not-contact.md` says so on the staff screen too.
    """
    text = raw.strip()
    international = text.startswith("+") or text.startswith("00")
    if text.startswith("00"):
        text = text[2:]
    # `+49 (0)30 …` is the standard German way of writing the trunk prefix that an international
    # caller drops. It is punctuation, not a digit.
    text = re.sub(r"\(\s*0\s*\)", "", text)
    digits = re.sub(r"\D", "", text)
    if len(digits) < 4:
        raise UnusableIdentifier("too few digits for a phone number")
    return f"+{digits}" if international else digits


def digest(kind: str, value: str, secret: str) -> str:
    """The stored form of one identifier: HMAC-SHA256 over `kind:normalised`, hex. Pure.

    The kind is inside the message, so the same string typed as two kinds gives two digests and a
    record of one kind can never block the other.
    """
    if not secret:
        raise MissingSecret(
            "DO_NOT_CONTACT_HMAC_KEY is empty: refusing to hash with an empty key "
            "(docs/do-not-contact.md)"
        )
    message = f"{kind}:{normalise(kind, value)}".encode()
    return hmac.new(secret.encode(), message, sha256).hexdigest()


def digests(kind: str, values: Iterable[str], secret: str) -> list[str]:
    """`digest` for several identifiers of one kind, in order."""
    return [digest(kind, value, secret) for value in values]


def blocked_positions(candidates: Sequence[str], recorded: Iterable[str]) -> list[bool]:
    """Which candidate digests are on the list. Pure, positional, and free of identifiers."""
    on_the_list = set(recorded)
    return [candidate in on_the_list for candidate in candidates]
