"""The pure half of onboarding by invite (ADR 0052, issue #115).

No model import, no database, no request: the token's shape, the documents a redemption must
present, the steps it goes through and the one refusal every failure shares. `accounts/invites.py`
is the half that writes, `marceline/app/invites.py` the half that answers HTTP.

Three rules live here because they are the ones that must not be re-decided per caller:

1. **A token is never stored.** `new_token()` returns the secret once; `token_hash()` is what a row
   keeps. Looking an invite up means hashing what arrived and comparing digests, which is also why
   an unknown token costs exactly as much work as a real one.
2. **One refusal.** `REFUSED` is the only thing any caller may say about a token that does not
   work — unknown, expired, revoked, already redeemed, or the feature switched off. A second
   sentence anywhere would turn the endpoint into an oracle for "was this address invited".
3. **The documents come from a setting, and empty refuses.** The pilot agreement and the service
   privacy notice are drafts until published. `ONBOARDING_DOCUMENTS` names the
   *published* version and digest of each; while it is empty the accept step refuses and no invite
   can be completed, so the flow cannot go live ahead of the documents by accident.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings

#: Bytes of entropy in an invite token. 32 bytes is what `delivery/tokens.py` uses for the export
#: tokens; there is no reason for an invite to be weaker, and the link is pasted, never typed.
TOKEN_BYTES = 32

#: What a token looks like once it is url-safe base64 without padding. Checked before any query, so
#: a malformed token never reaches the database — and it is refused with the same `REFUSED` as
#: every other failure, never with "malformed".
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

#: The one thing anybody says about a token that does not work. See rule 2 above.
REFUSED = "invite_not_available"

#: The two documents a redemption presents, in the order it presents them. The ids are stable —
#: a stored acceptance names one — so a new document is a new id, never a rename.
PILOT_AGREEMENT = "pilot_agreement"
SERVICE_PRIVACY_NOTICE = "service_privacy_notice"
REQUIRED_DOCUMENTS: tuple[str, ...] = (PILOT_AGREEMENT, SERVICE_PRIVACY_NOTICE)

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class Step(StrEnum):
    """Where a half-finished redemption stands. The order is the order of the screens.

    Derived from the row rather than stored: a stored cursor and the fields it claims to describe
    can disagree, and then one of them is a lie. `step_of` is the single definition.
    """

    PASSWORD = "password"
    DECLARATION = "declaration"
    DOCUMENTS = "documents"
    COUNTRY = "country"
    COMPLETE = "complete"
    DONE = "done"


@dataclass(frozen=True)
class PublishedDocument:
    """One document a redemption presents, exactly as `ONBOARDING_DOCUMENTS` names it.

    `sha256` pins the published text the link resolves to. An acceptance stores the digest, not the
    paragraphs: the text is a published artefact addressed by its digest, so a stored digest that
    matches nothing published any more is itself the finding.
    """

    document_id: str
    version: str
    sha256: str
    url: str


class DocumentsNotPublished(Exception):
    """`ONBOARDING_DOCUMENTS` does not name a published version of every required document."""


def new_token() -> str:
    """A fresh invite token. Shown once, at issue, and never recoverable afterwards."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """The digest a row keeps. SHA-256 of the token's UTF-8 bytes, lower-case hex.

    Deliberately not a password hash: the token is 256 bits of entropy from a CSPRNG, so there is
    nothing to brute-force and nothing a slow hash would buy — the same reasoning as
    `delivery/tokens.py`.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def token_well_formed(token: str) -> bool:
    return bool(TOKEN_SHAPE.match(token))


def invites_enabled() -> bool:
    """`ONBOARDING_INVITES_ENABLED`. Off means every route answers `REFUSED` and staff tooling
    refuses to issue: the flag is the switch, not a filter over existing rows."""
    return bool(getattr(settings, "ONBOARDING_INVITES_ENABLED", False))


def expiry_days() -> int:
    """`ONBOARDING_INVITE_EXPIRY_DAYS`, at least one day. A zero would issue invites that are
    already dead, which reads in the admin exactly like a bug in the clock."""
    return max(int(getattr(settings, "ONBOARDING_INVITE_EXPIRY_DAYS", 14)), 1)


def _parse_entry(entry: str) -> PublishedDocument:
    """`id:version:sha256:url`. The url may contain colons, so it is split off last."""
    parts = entry.strip().split(":", 3)
    if len(parts) != 4:
        raise DocumentsNotPublished(
            "ONBOARDING_DOCUMENTS entries are 'id:version:sha256:url' — got a malformed entry"
        )
    document_id, version, digest, url = (part.strip() for part in parts)
    if document_id not in REQUIRED_DOCUMENTS:
        raise DocumentsNotPublished(
            f"ONBOARDING_DOCUMENTS names an unknown document id; known ids: "
            f"{', '.join(REQUIRED_DOCUMENTS)}"
        )
    if not version or not url:
        raise DocumentsNotPublished(f"ONBOARDING_DOCUMENTS entry for {document_id} lacks a version")
    if not SHA256_HEX.match(digest.lower()):
        raise DocumentsNotPublished(
            f"ONBOARDING_DOCUMENTS entry for {document_id} needs the sha256 of the published text"
        )
    return PublishedDocument(
        document_id=document_id, version=version, sha256=digest.lower(), url=url
    )


def published_documents() -> tuple[PublishedDocument, ...]:
    """Every required document, in `REQUIRED_DOCUMENTS` order, or `DocumentsNotPublished`.

    This is the gate that keeps the flow behind the legal work: with the setting empty — its
    default — the accept step refuses, so an invite cannot be completed and nobody can be shown a
    draft as though it were the agreement.
    """
    configured = str(getattr(settings, "ONBOARDING_DOCUMENTS", "") or "").strip()
    if not configured:
        raise DocumentsNotPublished(
            "ONBOARDING_DOCUMENTS is empty: the pilot agreement and the service privacy notice "
            "are not published yet, so no invite can be completed yet"
        )
    found = {
        document.document_id: document
        for document in (_parse_entry(entry) for entry in configured.split(",") if entry.strip())
    }
    missing = [name for name in REQUIRED_DOCUMENTS if name not in found]
    if missing:
        raise DocumentsNotPublished(
            f"ONBOARDING_DOCUMENTS does not name a published version of: {', '.join(missing)}"
        )
    return tuple(found[name] for name in REQUIRED_DOCUMENTS)


def documents_available() -> bool:
    """Whether `published_documents()` would answer. Used to *describe* state, never to branch a
    refusal into a different sentence."""
    try:
        published_documents()
    except DocumentsNotPublished:
        return False
    return True
