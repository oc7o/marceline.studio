"""What development hands production: signed customer bundles and the accounts document (ADR 0028).

**A bundle is the customer API itself, not a copy of it.** Every document is produced by resolving
the customer API path through Django's URL configuration and calling the very view a signed-in
member of the organisation would reach — same ownership scoping, same confirmed-only and safety
filters, same schemas and the same JSON renderer. The bytes production later serves are therefore
the bytes development would have answered; there is no second serializer that could drift.

Documents per organisation: `originals`, `findings`, `cases`, `removals`, `rights` (ADR 0034, a
single overview document — no detail family, like `findings`), `verification` (ADR 0055) and
`data-requests` (ADR 0056), the same shape, `originals/{id}` and `cases/{id}`
for every original it owns (`ownership.owned_originals`), and `originals/*` / `cases/*` — the exact
404 an id that is not the organisation's gets, rendered for an id that cannot exist. **Document
keys carry the originals' public ids** (ADR 0033), exactly as the customer URL spells them, so
production matches a request path to a document without translating anything.

Notice drafts join them (ADR 0046): `notices`, `notices/{notice_id}` for every draft the customer's
own list shows (`app.notices.visible_draft_ids` — a draft the safety filter hides has no document,
so production cannot serve one either), `notices/candidates/{original_id}` per owned original, the
two `*` documents, and `notices/{notice_id}/export/{txt,eml,annex}`: the three download files, byte
for byte as development would answer them, including the 409 for a draft that is not approved or is
already closed. Rendering a bundle is **not** a download, so the `exported` audit event is not
written for one: `render` marks the request and the view formats without logging. The event for a
real download on production arrives afterwards, as an outbox item.

`page_ids` lists the **primary keys** of every page any document mentions, so a safety purge (which
development sends by primary key, `cache_purge.py`) can find the bundles that contain a page
without asking development. The documents name pages by public id only; `_page_ids` translates
those back. The signed envelope travels between our own machines only and never reaches a customer.

The export writes nothing of substance: the case file is read as the last `sync_cases` left it when
the export process runs with `CASE_SYNC_ON_READ=false` (runbook), and service tokens are only read.
The one exception is a missing original public id, which `ownership.public_ids_for` creates.
"""

import json
from collections.abc import Iterator
from datetime import datetime
from functools import lru_cache
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.contrib.auth.models import User
from django.http import HttpRequest
from django.urls import resolve

from accounts import account_actions, action_tokens, onboarding
from accounts.models import CustomerInvite, EmailVerification, Membership, Organisation
from core import public_ids
from delivery.envelope import (
    FORMAT_ACCOUNTS,
    FORMAT_BUNDLE,
    SCHEMA_VERSION,
    EnvelopeError,
    load_private_key,
    seal,
)
from delivery.store import CUSTOMER_CACHE_MAX_STALE
from index.models import Page
from marceline.app.coverage import monitored_sources
from marceline.app.notices import visible_draft_ids
from marceline.app.ownership import ORIGINAL_ID_PREFIX, owned_originals
from marceline.notices.export import FORMATS as NOTICE_FORMATS
from marceline.notices.models import NOTICE_ID_PREFIX

APP_PREFIX = "/api/v1/app/"
JSON_TYPE = "application/json"
#: The customer API version the documents follow (`marceline.api.api.version`).
API_VERSION = "1"
LIST_DOCUMENTS = (
    "originals",
    "findings",
    "cases",
    "removals",
    "rights",
    "notices",
    # Account-control verification (ADR 0055): one document per organisation. It carries this
    # organisation's claimed accounts and the live code of an open one — and nothing of anybody
    # else's, because it is rendered by calling the customer view as one of its own members.
    "verification",
    # The creator's own data requests (ADR 0056, issue #133): the same shape again, and the reason
    # it has to be here is the point of the feature — the creator who can reach only the public
    # host is exactly the one who could exercise neither Art. 15/20 nor Art. 17 from the product.
    "data-requests",
)
#: Detail families keyed by the public id of an original the organisation owns.
ORIGINAL_DETAIL_FAMILIES = ("originals", "cases", "notices/candidates")
#: Detail families keyed by the public id of one of the organisation's notice drafts (ADR 0046).
NOTICE_DETAIL_FAMILIES = ("notices",)
DETAIL_FAMILIES = ORIGINAL_DETAIL_FAMILIES + NOTICE_DETAIL_FAMILIES
#: Rendered for the `*` documents: a well-formed id that is never generated, per kind.
MISSING_ID = public_ids.missing(ORIGINAL_ID_PREFIX)
MISSING_NOTICE_ID = public_ids.missing(NOTICE_ID_PREFIX)
MISSING_BY_FAMILY: dict[str, str] = {
    **{family: MISSING_ID for family in ORIGINAL_DETAIL_FAMILIES},
    **{family: MISSING_NOTICE_ID for family in NOTICE_DETAIL_FAMILIES},
}
#: Statuses a customer read may answer with, and therefore a document may carry. 409 is here for
#: the notice downloads: a draft that is not approved, or already closed, has no file, and
#: production must answer that refusal rather than a 404 that would read as "no such draft".
DOCUMENT_STATUSES = (200, 404, 409)


@lru_cache(maxsize=4)
def _private_key(path: str) -> Ed25519PrivateKey:
    return load_private_key(path)


def signing_key() -> Ed25519PrivateKey:
    """Development's private key (`CACHE_SIGNING_KEY_PATH`); never committed, never on prod."""
    path = settings.CACHE_SIGNING_KEY_PATH
    if not path:
        raise EnvelopeError("signing_key_not_configured")
    try:
        return _private_key(path)
    except (OSError, ValueError) as exc:  # missing, or not a PEM key (e.g. /dev/null)
        raise EnvelopeError("signing_key_unreadable") from exc


def member_of(organisation: Organisation) -> User | None:
    """The account whose view of the customer API is the organisation's view.

    Every customer view scopes by `organisation_of(request.user)`, so any member renders the same
    documents; the oldest membership is picked for determinism. An organisation without members
    has nobody who could sign in to see a bundle, so it has none.
    """
    membership = (
        Membership.objects.select_related("user")
        .filter(organisation=organisation, user__is_active=True)
        .order_by("created_at", "pk")
        .first()
    )
    return membership.user if membership is not None else None


def render(path: str, user: User) -> tuple[int, str, bytes]:
    """Answer one customer API GET exactly as the live endpoint would, for this user.

    `bundle_render` marks the synthetic request so a view can tell a bundle from a customer's own
    request. Only one view reads it: the notice download, whose answer is the same either way but
    whose **audit event** belongs to a real download and not to building a cache entry.
    """
    request = HttpRequest()
    request.method = "GET"
    request.path = request.path_info = path
    request.META = {"REQUEST_METHOD": "GET", "SERVER_NAME": "export.internal", "SERVER_PORT": "80"}
    request.user = user
    request.bundle_render = True  # type: ignore[attr-defined]
    match = resolve(path)
    response = match.func(request, *match.args, **match.kwargs)
    return response.status_code, response["Content-Type"], response.content


def document_keys(organisation: Organisation) -> list[str]:
    ids = sorted(original.public_id for original in owned_originals(organisation))
    notice_ids = visible_draft_ids(organisation)
    keys = list(LIST_DOCUMENTS)
    for family in ORIGINAL_DETAIL_FAMILIES:
        keys.extend(f"{family}/{public_id}" for public_id in ids)
        keys.append(f"{family}/*")
    for family in NOTICE_DETAIL_FAMILIES:
        keys.extend(f"{family}/{public_id}" for public_id in notice_ids)
        keys.append(f"{family}/*")
    # The downloads (ADR 0046). Three small files per draft: a notice is text, and a customer who
    # opens the screen while development is down must still be able to take their notice away.
    for public_id in notice_ids:
        keys.extend(f"notices/{public_id}/export/{fmt}" for fmt in NOTICE_FORMATS)
    return keys


def _path_for(key: str) -> str:
    """The customer API path whose answer is this document's body."""
    family = key.rsplit("/", 1)[0] if key.endswith("/*") else key
    return APP_PREFIX + key.replace("*", MISSING_BY_FAMILY.get(family, MISSING_ID))


def _page_public_ids(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "page_id" and isinstance(item, str):
                yield item
            else:
                yield from _page_public_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _page_public_ids(item)


def _page_ids(public: set[str]) -> set[int]:
    """The primary keys of the pages these public ids name; a purge addresses pages by key."""
    if not public:
        return set()
    return set(Page.objects.filter(public_id__in=public).values_list("pk", flat=True))


def bundle_payload(organisation: Organisation, *, now: datetime) -> dict[str, Any] | None:
    """The unsigned bundle of one organisation, or None when it has no member to render for."""
    user = member_of(organisation)
    if user is None:
        return None
    documents: dict[str, dict[str, Any]] = {}
    page_public_ids: set[str] = set()
    for key in document_keys(organisation):
        status, content_type, body = render(_path_for(key), user)
        if status not in DOCUMENT_STATUSES:
            raise EnvelopeError(f"render_status_{status}")
        text = body.decode()
        documents[key] = {"status": status, "content_type": content_type, "body": text}
        # Only a JSON document can name a page; the notice downloads are plain text and an
        # unsent message, and every page they report is already named by the draft's own document.
        if status == 200 and content_type.startswith(JSON_TYPE):
            page_public_ids.update(_page_public_ids(json.loads(text)))
    return {
        "format": FORMAT_BUNDLE,
        "schema_version": SCHEMA_VERSION,
        "organisation_id": organisation.pk,
        "as_of": now,
        "expires_at": now + CUSTOMER_CACHE_MAX_STALE,
        "api_version": API_VERSION,
        "documents": documents,
        "page_ids": sorted(_page_ids(page_public_ids)),
    }


def signed_bundle(organisation_id: int, *, now: datetime) -> bytes | None:
    """Wire bytes for one organisation's bundle; None for an unknown or member-less organisation."""
    organisation = Organisation.objects.filter(pk=organisation_id).first()
    if organisation is None:
        return None
    payload = bundle_payload(organisation, now=now)
    return None if payload is None else seal(payload, signing_key())


def _open_invites(*, now: datetime) -> list[dict[str, Any]]:
    """The open invites production must be able to refuse a token against (ADR 0052 §9, slice 2).

    Only while `ONBOARDING_INVITES_ENABLED`, and the key is **absent** from the document otherwise —
    a production still running the previous image rejects a document with a field it does not know
    (the payload forbids extras), and switching the flag on is a deliberate act after the promotion.

    Per open invite: the token's **digest**, the expiry, the address the account will have and the
    organisation it leads into. Never the token, never the note, never who issued it. The address is
    there rather than a hash of it because production runs Django's password validators against it
    (`UserAttributeSimilarityValidator`), so a hash would make the two hosts refuse different
    passwords, and because it is the same string production will hold as `User.username` as soon as
    the account exists — an invite only brings it a few days earlier, and only while it is open.
    """
    if not onboarding.invites_enabled():
        return []
    rows = CustomerInvite.objects.filter(
        revoked_at__isnull=True, redeemed_at__isnull=True, expires_at__gt=now
    ).order_by("pk")
    return [
        {
            "token_hash": invite.token_hash,
            "email": invite.email,
            "expires_at": invite.expires_at,
            "organisation_id": invite.organisation_id or 0,
            "organisation_name": invite.organisation_name,
        }
        for invite in rows
    ]


def _open_action_tokens(*, now: datetime) -> list[dict[str, Any]]:
    """The mailed links production must be able to answer for (ADR 0054 §1).

    Only while `ACCOUNT_EMAIL_FLOWS_ENABLED`, and the key is **absent** otherwise — the same rule as
    the invites section, for the same reason: the payload forbids extras, so a production still
    running the previous image would refuse a whole document it did not know.

    Per open token: its **digest**, the kind, the expiry and the account's id (development's primary
    key, which production already keys users by). Never the token, never the address — production
    reads the address off its own replicated user row, which is what lets it run Django's password
    validators against the same account development would.
    """
    if not action_tokens.flows_enabled():
        return []
    return [
        {
            "token_hash": row.token_hash,
            "kind": row.kind,
            "user_id": row.user_id,
            "expires_at": row.expires_at,
        }
        for row in account_actions.open_tokens(now=now)
    ]


def accounts_payload(*, now: datetime) -> dict[str, Any]:
    """Everyone production needs to sign customers in, and nothing else (ADR 0028 table).

    Only users with a membership, and only what login and `/app/me` read: username, password
    hash, active flag, organisation and role. Staff and superuser flags are never exported —
    production has no staff.

    With `ONBOARDING_INVITES_ENABLED` it also carries the open invites, so the public host can
    refuse an unknown or expired token itself without asking development anything (ADR 0052 §9).
    A revoked invite simply drops out of the next document.

    With `ACCOUNT_EMAIL_FLOWS_ENABLED` it carries the open mailed links the same way (digests,
    kinds and expiries, ADR 0054) and one more field per user: when that address was confirmed.
    Both are absent while their flag is off, so a production on an older image never meets a field
    it forbids.
    """
    organisations = [
        {
            "id": organisation.pk,
            "public_id": organisation.public_id,
            "name": organisation.name,
            "segment": organisation.segment,
            "created_at": organisation.created_at,
        }
        for organisation in Organisation.objects.order_by("pk")
    ]
    # The verification state travels per user and **only while the flows are on** (ADR 0054), like
    # the sections below: an older production forbids a field it does not know and would refuse the
    # whole document, taking every account update with it.
    verified: dict[int, datetime] = {}
    if action_tokens.flows_enabled():
        verified = {
            user_id: moment
            for user_id, moment in EmailVerification.objects.filter(
                verified_at__isnull=False
            ).values_list("user_id", "verified_at")
            if moment is not None
        }
    users = []
    for membership in Membership.objects.select_related("user").order_by("user_id"):
        row: dict[str, Any] = {
            "id": membership.user.pk,
            "username": membership.user.get_username(),
            "password": membership.user.password,
            "is_active": membership.user.is_active,
            "organisation_id": membership.organisation_id,
            "role": membership.role,
            "membership_created_at": membership.created_at,
        }
        if action_tokens.flows_enabled():
            row["email_verified_at"] = verified.get(membership.user.pk)
        users.append(row)
    payload: dict[str, Any] = {
        "format": FORMAT_ACCOUNTS,
        "schema_version": SCHEMA_VERSION,
        "as_of": now,
        "expires_at": now + CUSTOMER_CACHE_MAX_STALE,
        "monitored_sources": monitored_sources(),
        "organisations": organisations,
        "users": users,
    }
    invites = _open_invites(now=now)
    if invites or onboarding.invites_enabled():
        payload["invites"] = invites
    if action_tokens.flows_enabled():
        payload["action_tokens"] = _open_action_tokens(now=now)
    return payload


def signed_accounts(*, now: datetime) -> bytes:
    return seal(accounts_payload(now=now), signing_key())
