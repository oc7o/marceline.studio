"""Typed responses of the customer API.

Two conventions here are not style, they are correctness:

**Every class name is unique across the whole API.** Ninja keys OpenAPI components by class name,
so a customer `OriginalOut` next to the review `OriginalOut` does not merely look confusing — it
renames the *review* components in `openapi.json` and therefore in the moderation SPA's generated
types. Hence the `Customer…` / `App…` prefixes where the review API already owns the obvious name.

**Ids are public ids, never primary keys** (issue #77, ADR 0033): `orig_…` for an original,
`page_…` for the page a finding sits on, `org_…` for the organisation — opaque strings, so no
response says how many rows exist or how fast they grow. `PUBLIC_ID_DESCRIPTION` says so in the
OpenAPI document.

**No media fields anywhere**, by decision: phase 1 shows no pirate imagery (issue #53), so a media
id must not be constructible from a customer response. Adding one is an ADR, not a schema edit.
"""

from datetime import date, datetime
from typing import Literal

from ninja import Field, Schema

from catalog.hosting.history import RemovalTime
from catalog.hosting.jurisdiction import Jurisdiction

Segment = Literal["professional", "enterprise"]
Role = Literal["owner", "member"]
# Three states per original, as the customer sees them (docs/customer-ui-plan.md §1).
#
# **Only confirmed findings exist for a customer** (operator decision, 2026-09-17). An unconfirmed
# match is not listed, not counted and not dated: what we are still looking at, and how fast we
# look, is internal. For the same reason nothing here says *when* we first noticed a page or *when*
# on a day we confirmed it — `confirmed_on` is a date, and the crawler's first-seen and discovery
# stamps are not in any customer schema, so no pair of fields spells out our review latency.
OriginalState = Literal["nothing_found", "found_online", "found_removed"]
#: What "why we think this is yours" rests on (`originals.evidence_of`). Deliberately no more than
#: that: confidence bands and matched frame counts are how we process, and customers get no
#: insight into confidence or processing (operator decision, 2026-09-17).
CustomerEvidence = Literal["fingerprint_match", "no_fingerprint_match"]
#: How much of the original a copy carries, as far as the fingerprints show it (issue #62,
#: `originals.extent_of`): the whole video, a part of it, or not determined — poster-only evidence,
#: a possible compilation and anything else a person still has to judge. A fact about the copy,
#: never a confidence, and never a reason a copy is or is not a finding: excerpts are findings.
CustomerCopyExtent = Literal["full_copy", "excerpt", "not_determined"]
#: What shape of work an original is (`matching.Reference.kind`, ADR 0020): a video along a
#: timeline, or a photo collection with none. Sent as a fact rather than inferred from a null
#: duration, because the UI's words depend on it (issue #116, ADR 0053) and an inference that is
#: right today is the kind that goes quietly wrong later.
CustomerOriginalKind = Literal["video", "photos"]

# The case file (docs/customer-ui-plan.md §3). Everything below is a **machine-readable code**:
# the product is bilingual from the start, so a German or English sentence in a payload is a bug —
# the UI owns every word the customer reads, in their language.
CaseState = Literal["open", "removed_watching"]
#: The steps of §3, in order. They are the whole vocabulary; a UI may render only these. "Found"
#: and "verified" are one step for a customer, `confirmed`, dated to the day (2026-09-17).
CaseStep = Literal[
    "confirmed",
    "notice_prepared",
    "notice_sent",
    "notice_acknowledged",
    "removed",
    "rechecked",
]
#: `done` has a date. `pending` may still happen. `unavailable` cannot happen yet, and says why.
CaseStepStatus = Literal["done", "pending", "unavailable"]
#: Why something is unavailable or unknown — never prose, and never a guess dressed up as data.
CaseUnavailableReason = Literal[
    # Nothing in the system produces this yet.
    "capability_not_built",
    # Blocked on a pending legal review.
    "legal_review_pending",
    # It could only follow a step that is itself unavailable.
    "prerequisite_unavailable",
]
#: Facts the case file is expected to carry one day and honestly does not carry today. Named, so
#: the UI can render "not yet known" for exactly this list instead of an empty object that reads
#: like data (docs/customer-ui-plan.md §3). Since ADR 0027 hoster metadata and jurisdiction are
#: carried per finding (`hosting`) and no longer listed; the codes stay in the vocabulary.
CaseUnknownTopic = Literal[
    "hoster_metadata",
    "jurisdiction",
    "cooperation_history",
    "notice_preparation",
    "notice_delivery",
    "reupload_watch_coverage",
]
RemovalState = Literal["removed", "reopened"]
#: How many sources we monitor, as a floor bucket (`marceline/app/coverage.py`). Never a count and
#: never a list: which sites we crawl is not shown to customers (operator decision, 2026-09-16).
MonitoredSources = Literal[
    "fewer_than_10", "10_plus", "50_plus", "100_plus", "250_plus", "500_plus", "1000_plus"
]

# Hoster intelligence (ADR 0027). Codes again: the UI turns `eu` into "Suggested notice channel:
# DSA Article 16" or "Vorgeschlagener Meldekanal: DSA Art. 16", not the API — a suggestion, never
# a statement of which law applies (product wording rule).
# `Jurisdiction` (eu / us / other / unknown) and `RemovalTime` (same_day / 1_plus_days / … /
# 30_plus_days) are defined next to the functions that derive them.
ORIGINAL_ID_DESCRIPTION = (
    "Opaque public id of the original (`orig_` + 32 hex). Stable for as long as this organisation "
    "owns the file; never a database key."
)
PAGE_ID_DESCRIPTION = (
    "Opaque public id of the page the copy sits on (`page_` + 32 hex); never a database key."
)
EVIDENCE_DESCRIPTION = (
    "`fingerprint_match`: the fingerprints of the page's preview images match this original. "
    "`no_fingerprint_match`: a person reviewed the match without an automatic fingerprint match. "
    "No confidence, no frame counts."
)
EXTENT_DESCRIPTION = (
    "`full_copy`: the page carries the whole original. `excerpt`: only a part of it. "
    "`not_determined`: the evidence does not show how much (a single preview image, or a page "
    "a person still has to judge). Every extent is a finding; none is a confidence."
)

#: `not_looked_up`: no record for this host yet — every other field is then null or empty.
HostLookupState = Literal["complete", "partial", "failed", "not_looked_up"]
#: Where the abuse contact came from: the IP network's RDAP abuse role, or the domain's registrar.
AbuseContactSource = Literal["ip_network", "registrar"]


class AppErrorOut(Schema):
    detail: str


class CustomerLoginIn(Schema):
    """Credentials for `POST /api/v1/app/auth/login`. There is no registration counterpart."""

    username: str
    password: str


class CustomerSessionOut(Schema):
    """Whether this browser currently holds a session, and the CSRF cookie that comes with it."""

    authenticated: bool
    username: str | None = None


class OrganisationOut(Schema):
    id: str = Field(description="Opaque public id of the organisation (`org_` + 32 hex).")
    name: str
    segment: Segment = Field(description="Billing shape only; it changes nothing about access.")
    role: Role = Field(description="The signed-in user's role; no permissions attached yet.")
    created_at: datetime


class AccountOut(Schema):
    username: str
    organisation: OrganisationOut | None = Field(
        description="Null when this account belongs to no organisation and therefore owns nothing."
    )
    monitored_sources: MonitoredSources = Field(
        description="Rough size of our coverage as a floor bucket, the same for every account. "
        "Never the count and never the sites."
    )
    can_add_originals: bool = Field(
        default=False,
        description="Whether this account may add an original here (`POST /app/fingerprints`): "
        "a member of an organisation, on a machine that takes uploads (production: once its "
        "outbox is configured, where an upload is accepted first and added shortly after).",
    )
    can_upload_media: bool = Field(
        default=False,
        description="Whether this account may send the file itself instead of hashing it locally "
        "(`POST /app/uploads`, ADR 0044). False unless the feature is switched on and a spool is "
        "configured, and always false on the public host. Local hashing is the default either way.",
    )
    can_export_data: bool = Field(
        default=False,
        description="Whether this account may download its own data (`GET /app/export`, ADR "
        "0045). A member of an organisation, on a machine that holds it — development only, "
        "always false on the public host, like rights and notices.",
    )
    can_verify_account: bool = Field(
        default=False,
        description="Whether this account is offered the account-control step (ADR 0055): a "
        "one-time code the creator places in the profile of the platform account whose content we "
        "protect, which a staff member then looks at by hand. False unless the feature is switched "
        "on for this machine; unlike the upload routes it may be true on the public host.",
    )
    can_request_data: bool = Field(
        default=False,
        description="Whether this account may ask for an export of its data or for its deletion "
        "(`/app/data-requests`, ADR 0056). Asking is not the same as downloading: a request is "
        "recorded and answered by a person, and it may be true on the public host — which is the "
        "whole point, since `can_export_data` never is.",
    )
    email_verified: bool = Field(
        default=False,
        description="Whether this account's address has been confirmed from a mailed link (ADR "
        "0054). It **blocks nothing**: staff issued the invite to this address, so the app asks in "
        "a banner and refuses no screen over it. False while the flows are switched off.",
    )
    can_verify_email: bool = Field(
        default=False,
        description="Whether a verification mail can be asked for here (`POST "
        "/app/auth/verify-email`): the flows are on, a transport is configured, and this account "
        "has an organisation. False hides the banner's button rather than offering a dead end.",
    )


class FindingCountsOut(Schema):
    confirmed: int = Field(
        description="Human-reviewed matching copies of this original (flagged pages excluded). "
        "Matches nobody has reviewed are never counted."
    )
    removed: int = Field(description="Human-reviewed matching copies whose page is gone.")


class CustomerOriginalOut(Schema):
    id: str = Field(description=ORIGINAL_ID_DESCRIPTION)
    title: str
    kind: CustomerOriginalKind = Field(
        description="`video`, or `photos` for a photo collection, which has no duration."
    )
    duration_s: float | None
    created_at: datetime
    state: OriginalState
    counts: FindingCountsOut


class FindingOut(Schema):
    """One page carrying this original. Text evidence only — never an image, never a thumbnail."""

    page_id: str = Field(description=PAGE_ID_DESCRIPTION)
    site: str = Field(description="Domain the copy sits on.")
    url: str = Field(description="The matched page itself, as text; the UI never embeds it.")
    availability: str = Field(description="playable / removed / unknown.")
    last_checked: datetime | None = Field(description="Last availability check; null = never.")
    evidence: CustomerEvidence = Field(description=EVIDENCE_DESCRIPTION)
    extent: CustomerCopyExtent = Field(description=EXTENT_DESCRIPTION)


class CustomerOriginalDetailOut(Schema):
    original: CustomerOriginalOut
    findings: list[FindingOut] = Field(description="Human-reviewed matching copies only.")


class CaseStepOut(Schema):
    """One step of the record's timeline: a code, a status and a date. Never a sentence.

    A `done` step carries exactly one of `at` (a moment: the external steps) or `on` (a day: only
    `confirmed`, which is ours and deliberately no more precise). `pending` means the step may
    still happen; `unavailable` means nothing in the system can produce it yet and `reason` says
    which kind of gap that is — a missing capability, an open legal question, or a step that could
    only follow another unavailable one.
    """

    step: CaseStep
    status: CaseStepStatus
    at: datetime | None = Field(description="When it happened; null unless a `done` external step.")
    on: date | None = Field(
        default=None, description="The day it happened; set only for a `done` `confirmed` step."
    )
    reason: CaseUnavailableReason | None = Field(
        default=None, description="Set only for `unavailable`."
    )
    blocked_by_issue: int | None = Field(
        default=None, description="Issue that has to be answered first, where there is one."
    )


class CaseUnknownOut(Schema):
    """A fact the record does not carry today, named so the UI can render "not yet known".

    Rather than an empty object that later reads like data, the record names the absence.
    """

    topic: CaseUnknownTopic
    reason: CaseUnavailableReason
    blocked_by_issue: int | None = None


class HostAbuseContactOut(Schema):
    email: str
    source: AbuseContactSource
    source_url: str = Field(description="The RDAP record the contact was read from.")


class HostRegistrarOut(Schema):
    name: str
    abuse_email: str | None
    source_url: str = Field(description="The RDAP record the registrar was read from.")


class CustomerResponseHistoryOut(Schema):
    """A provider's past notices, deliberately coarse (ADR 0027 amendment 2026-09-17)."""

    removed_percent: int = Field(
        description="Share of observed notices after which the content was removed, rounded to "
        'the nearest 5 (shown as "about"); never an exact rate.'
    )
    usual_removal_time: RemovalTime | None = Field(
        description="Median time from notice to removal, floored into a bucket; null when too few "
        "removals carry a date."
    )


class CustomerHostOut(Schema):
    """Who serves one hostname and whom to complain to, from DNS and RDAP registries (ADR 0027)."""

    hostname: str
    lookup: HostLookupState
    looked_up_at: datetime | None = Field(description="Null when never looked up.")
    stale: bool = Field(description="Older than the refresh window; shown, but may have changed.")
    addresses: list[str]
    asn: int | None
    as_name: str | None
    provider: str | None = Field(description="Registrant organisation of the IP network.")
    country: str | None = Field(description="ISO 3166-1 alpha-2 of the IP allocation.")
    jurisdiction: Jurisdiction = Field(
        description="Code of the suggested notice channel, from the country of the IP allocation "
        "(behind a CDN, the CDN's). A suggestion only, never a statement of which law applies."
    )
    cdn: str | None = Field(description="CDN/proxy code in front (e.g. cloudflare); null = none.")
    abuse_contact: HostAbuseContactOut | None
    registrar: HostRegistrarOut | None
    response_history: CustomerResponseHistoryOut | None = Field(
        description="What happened to past notices recorded with this provider, rounded; null "
        'until enough notices have been observed ("no response history yet"). Past tense '
        "only: not a forecast. Exact counts and durations are internal."
    )


class CustomerFindingHostingOut(Schema):
    """Where one finding sits: the page's own host, and the hosts its previews are served from."""

    page_host: CustomerHostOut
    content_hosts: list[CustomerHostOut] = Field(
        description="Other hosts serving this page's previews and stills; hostnames only."
    )


class CustomerCaseFindingOut(Schema):
    """One human-reviewed matching copy of this original, with the record's stamps. Text only."""

    original_id: str = Field(description=ORIGINAL_ID_DESCRIPTION)
    page_id: str = Field(description=PAGE_ID_DESCRIPTION)
    site: str = Field(description="Domain the copy sits on.")
    url: str = Field(description="The matched page itself, as text; the UI never embeds it.")
    case_state: CaseState
    availability: str = Field(description="playable / removed / unknown.")
    online: bool | None = Field(description="Null when the last check could not tell.")
    last_checked: datetime | None = Field(description="Last availability check; null = never.")
    confirmed_on: date = Field(
        description="The day a person reviewed the match with this original."
    )
    removed_at: datetime | None = Field(description="First check that found the page gone.")
    reappeared_at: datetime | None = Field(description="This URL is live again after a removal.")
    evidence: CustomerEvidence = Field(description=EVIDENCE_DESCRIPTION)
    extent: CustomerCopyExtent = Field(description=EXTENT_DESCRIPTION)
    hosting: CustomerFindingHostingOut


class CustomerCaseOut(Schema):
    """The record of one original: living state, not an archive — `removed_watching` can reopen."""

    original_id: str = Field(description=ORIGINAL_ID_DESCRIPTION)
    title: str
    state: CaseState
    opened_on: date = Field(description="The day the first match was human-reviewed.")
    removed_at: datetime | None = Field(
        description="When the record last became fully removed; null while anything is up."
    )
    reopened_on: date | None = Field(description="The day something was last up again.")
    reopen_count: int = Field(description="How often it reopened after having been fully removed.")
    counts: FindingCountsOut


class CustomerCaseDetailOut(Schema):
    original: CustomerOriginalOut
    case: CustomerCaseOut | None = Field(
        description="Null while nothing has been found: there is no record yet."
    )
    findings: list[CustomerCaseFindingOut] = Field(
        description="Human-reviewed matching copies only."
    )
    timeline: list[CaseStepOut] = Field(description="Always all six steps, in order.")
    not_available: list[CaseUnknownOut] = Field(
        description="What this record honestly does not know yet."
    )


class CustomerRemovalOut(Schema):
    """One entry of the removals log: something that came down, and whether it stayed down.

    Only human-reviewed matches appear. A page nobody reviewed as a match of this original may have
    vanished for any reason, and listing it as a removal would be a claim the data does not support.
    """

    original_id: str = Field(description=ORIGINAL_ID_DESCRIPTION)
    title: str
    page_id: str = Field(description=PAGE_ID_DESCRIPTION)
    site: str
    url: str
    removed_at: datetime
    confirmed_on: date
    reappeared_at: datetime | None = Field(description="Set when this URL came back.")
    last_checked: datetime | None
    state: RemovalState
    case_state: CaseState
    handled: bool = Field(
        description="Removed, not back at this URL, and nothing else of this original is up. "
        "Only ever as true as the sites we watch — see `not_available` on the record."
    )


# Invoices (ADR 0032): the organisation's issued billing documents. Codes only; the UI owns the
# words. Drafts never appear. Amounts are integer euro cents.
InvoiceKind = Literal["invoice", "cancellation"]
#: `issued` is a cancellation document's standing: it asks for no payment of its own.
InvoiceStatus = Literal["open", "overdue", "paid", "cancelled", "issued"]
InvoiceVatTreatment = Literal["domestic", "reverse_charge", "small_business"]


class CustomerInvoiceOut(Schema):
    """One issued invoice or cancellation document. The PDF is `GET …/invoices/{id}/pdf`."""

    id: str = Field(
        description="Opaque public id of the document (`inv_` + 32 hex); never a database key."
    )
    number: str = Field(
        description="The legal invoice number, as printed on the document (sequential by law)."
    )
    kind: InvoiceKind
    status: InvoiceStatus
    issued_on: date
    due_on: date | None = Field(description="Payment due date; null for a cancellation.")
    period_start: date | None
    period_end: date | None
    currency: Literal["EUR"]
    net_cents: int
    vat_cents: int
    gross_cents: int = Field(description="Negative for a cancellation.")
    vat_treatment: InvoiceVatTreatment
    paid_on: date | None = Field(description="When the transfer was recorded as received.")
    cancels_number: str | None = Field(description="The invoice a cancellation cancels.")
    cancelled_by_number: str | None = Field(description="The cancellation of this invoice.")


# --- Onboarding by invite (ADR 0052, issue #115) ------------------------------------------------
# The one customer surface reachable without a session. Every refusal is the same `AppErrorOut`
# with `detail = "invite_not_available"` — unknown, expired, revoked, redeemed and "switched off"
# must be indistinguishable, or the endpoint becomes an oracle for "was this address invited".

InviteStep = Literal["password", "declaration", "documents", "country", "complete", "done"]


class InviteDocumentOut(Schema):
    """One document the redemption presents, as `ONBOARDING_DOCUMENTS` publishes it."""

    document_id: str
    version: str
    sha256: str = Field(description="Of the published text the URL resolves to.")
    url: str


class InviteStateOut(Schema):
    """Where a redemption stands, and everything the screen needs to render the current step.

    Codes and data only — the words are the bilingual UI's (`backend/marceline/CLAUDE.md`). The
    declarations are the exception and travel as text: each is a wording somebody signs, the German
    is the operative language (`accounts/declarations.py`), and translating one in the UI would
    create a second thing that could have been agreed.
    """

    step: InviteStep
    email: str = Field(description="Fixed by the invite; it becomes the username.")
    organisation_name: str
    documents: list[InviteDocumentOut]
    documents_published: bool = Field(
        description="False while ONBOARDING_DOCUMENTS names no published version: the accept step "
        "then refuses and the invite cannot be completed (ADR 0052 §3)."
    )
    declaration_version: str
    declaration_text: str
    age_declaration_version: str = Field(
        description="The majority and legal-capacity declaration (issue #123), versioned like the "
        "Unternehmer one."
    )
    age_declaration_text: str
    declaration_language: str = Field(
        description="The operative language of both declarations. German: the agreement is German."
    )
    password_min_length: int
    eu_countries: list[str]
    outside_eu: bool = Field(
        default=False,
        description="The last country answer was outside the pilot's scope. The invite is not "
        "consumed by it and the step can be answered again.",
    )
    account_step_next: bool = Field(
        default=False,
        description="The account-control step (ADR 0055) is switched on, so the finished panel "
        "names it as what comes next. A global fact about this deployment, never about the token.",
    )


class InvitePasswordIn(Schema):
    password: str


class InviteDeclarationIn(Schema):
    """Both declarations of the one declaration step, each with the version that was displayed.

    The versions travel back so a page left open across a change to `accounts/declarations.py`
    cannot sign wording nobody is showing any more — the same rule, and the same refusal code, as
    the rights declarations (`marceline/rights/services.py`).

    **Every field has a default although none of them is optional.** A required field would be
    rejected by the schema *before* the view runs, so a body missing one would answer 422 where the
    view answers the neutral 404 — the token check has to come first, always (ADR 0052 §6). Missing
    reads as absent and the view refuses it with an ordinary problem code.
    """

    accepted: bool = Field(
        default=False, description="The Unternehmer declaration, ticked by the customer."
    )
    declaration_version: str = Field(
        default="", description="The Unternehmer version the screen displayed."
    )
    age_accepted: bool = Field(
        default=False,
        description="The majority and legal-capacity declaration, ticked by the customer.",
    )
    age_declaration_version: str = Field(
        default="", description="The majority version the screen displayed."
    )


class InviteAcceptIn(Schema):
    accepted: list[str] = Field(description="The document ids that were ticked; all are required.")
    language: str = Field(default="", description="Which language the documents were presented in.")


class InviteCountryIn(Schema):
    country: str = Field(description="ISO 3166-1 alpha-2, where the business is established.")


# --- the mailed account actions (ADR 0054) -------------------------------------------------------
#
# Everything a stranger can reach without a session answers the same thing whatever happened, which
# is why the request schema has defaults for every field: a missing one would be a 422 *before* the
# view runs, and a 422 where the neutral answer belongs is the whole enumeration hole in one status
# code (the same rule as `InviteDeclarationIn`).


class PasswordResetRequestIn(Schema):
    """ "Send me a link". Nothing here is optional and everything has a default — see above."""

    email: str = Field(
        default="", max_length=254, description="The address to mail, if it has an account."
    )
    language: str = Field(default="", max_length=8, description="Which language to write in.")


class AccountMailRequestedOut(Schema):
    """The one answer to a request for a mailed link (ADR 0054 §2).

    It is not a statement that a mail was sent: an address with no account, a staff account, an
    inactive one and a transport that refused all end here, and so does a request on the public host
    that has only been accepted. The screen says "if this address has an account, a mail is on its
    way", which is true in every one of those cases.
    """

    detail: str = Field(description="`mail_requested`, always.")


class AccountLanguageIn(Schema):
    """Which language a mail this request triggers is written in. Defaulted, never required."""

    language: str = Field(default="", max_length=8)


class PasswordResetStateOut(Schema):
    """What the set-a-new-password screen needs. Only reachable with a working link."""

    email: str = Field(description="The account the link belongs to; it cannot be changed here.")
    password_min_length: int


class PasswordResetIn(Schema):
    password: str = Field(default="", description="The new password. Django's validators decide.")


class PasswordResetDoneOut(Schema):
    """The password is set (development) or on its way to being set (the public host, ADR 0028).

    Nobody is signed in either way: the mailed link proves the reader has the inbox, not that they
    know the new password, and the next step is the sign-in form. `pending` is the honest difference
    — on the public host the new password reaches it with the next `pull_accounts`.
    """

    pending: bool = Field(default=False, description="True on the public host: being applied.")
    submission_id: str = Field(default="", description="The outbox item (sub_…), when pending.")


class EmailVerificationStateOut(Schema):
    email: str = Field(description="The address this link confirms.")


class EmailVerifiedOut(Schema):
    """The address is confirmed (development) or the confirmation is on its way (public host)."""

    verified: bool = Field(default=False)
    pending: bool = Field(default=False)
    submission_id: str = Field(default="", description="The outbox item (sub_…), when pending.")


class PasswordProblemsOut(Schema):
    """Why a new password was not accepted, as codes (`accounts/passwords.py`). Never a sentence."""

    problems: list[str]


class InviteAcceptedOut(Schema):
    """Production's answer to a finished redemption it cannot apply itself (ADR 0052 §9).

    The public host creates no account (ADR 0028): the redemption is accepted into the outbox and
    development applies it within about a minute, after which `pull_accounts` carries the account
    back. So this says exactly what has happened — the redemption was taken — and never that the
    account exists. The screen tells the creator to try signing in in a few minutes.
    """

    pending: bool = Field(default=True, description="Accepted; the account is being set up.")
    submission_id: str = Field(
        description="The outbox item carrying it (sub_…), for an operator looking into a "
        "redemption that never produced an account. Nothing a customer can query."
    )


class InviteProblemsOut(Schema):
    """Why a step was not accepted, as codes. Never a sentence, never a reason about the token."""

    problems: list[str]
