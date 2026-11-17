# 0055 — Identity and majority by proof of control of the creator's platform account

Status: accepted (operator decision, 2026-09-19). Behind `ACCOUNT_CONTROL_VERIFICATION_ENABLED`,
off by default. Builds on ADR 0034 (the rights model and its `identity_not_verified` blocker),
ADR 0052 and its 2026-09-19 amendment (the declarations at onboarding), ADR 0028 and ADR 0046 (what
reaches the public host: reads through signed bundles, writes through the outbox).

## Context

The notice gate has a blocker called `identity_not_verified`, and until now the only way to clear it
was a staff member recording, in the admin, that they had checked *something*: a commercial-register
extract, a company email address, or a random token the customer was asked to "place in a profile
you control and tell us where" (`RightsClaimant.account_control_token`). That token had no platform,
no expiry, no record of what was looked at and no customer screen — the creator was expected to
mention it in an email.

Two things had to be decided at once, and the operator decided them together on 2026-09-19:

- **what we check.** The pilot is B2B and EU-only (issue #95), a contract with a minor is voidable,
  and every case file rests on the customer being who they say they are. Keeping ID
  copies is the *worst* option available — Art. 5(1)(c) GDPR argues against the whole idea — while
  account control is among the controls that actually reduce the risk
  this check exists for;
- **how we check it.** The creator platforms our customers publish on run their own identity checks,
  including that the account holder is of age, before an account may earn money. So control of such
  an account carries both facts we need:
  who this is, and that they are of age. **This is our operating assumption**, and
  it is written down here so that a later reader knows exactly what the check does and does not
  prove.

## Decision

### 1. What is recorded, and what is never recorded

**We never store an identity document, a date of birth or an age document.** Not a copy, not a
number, not an expiry date, not a scan "for the file". There is no field for one, and the note field
staff may fill says so in its help text. This is the operator's decision of 2026-09-19 and it is
load-bearing for everything else here.

What is recorded is **proof of control**: we show the creator a one-time code, they place it for a
short time in the bio of the platform account whose content we protect (or send it to us by direct
message from that account), a staff member opens that **public** profile **by hand, in their own
browser**, reads the code with their own eyes and records the outcome.

### 2. The model

`marceline.rights.AccountControlChallenge` — one row per organisation × claimed platform account:

| what | why |
|---|---|
| `platform` from a fixed list, `handle` | "which platform" is the fact the whole check rests on, so it is a choice and never free text. Each platform carries its own hosts (`rights/account_control.py`). |
| `profile_url`, optional | **inert text.** Displayed to staff, copied by a human, never fetched. |
| `state` | `pending` → `placed` → `verified` / `rejected`, plus `expired` and `revoked`. |
| `code`, `code_issued_at`, `code_expires_at`, `issue_count` | one live code per claim; a reissue overwrites it, which is what invalidates the previous one. |
| `placed_at`, `placed_by` | the creator's own statement that the code is up. It verifies nothing. |
| `method`, `decided_at`, `decided_by`, `note` | the outcome, attributed. `note` is **never required**. |

`AccountControlEvent` is the append-only log — `issued`, `shown`, `placed`, `verified`, `rejected`,
`expired`, `revoked` — with **UPDATE refused by a database trigger** (`marceline/0027`), the same
technique as `WorkRightsTransition` (ADR 0034) and `NoticeEvent` (ADR 0035). `issued` is a staff
member preparing a code; `shown` is the creator asking for their own and being given it — the only
moment we can honestly record that the code reached them, and it is a write they made, never a read
that wrote (ADR 0028 forbids a write on a cached read).

**Nothing in this flow may fetch anything.** `rights/account_control.py`,
`account_control_services.py` and `rights/models.py` import no HTTP client, and a test fails if one
of them ever does. A URL a stranger typed, opened by our code, is the same hole ADR 0042 keeps shut
for page content ("an extracted URL is a fetch instruction written by the page"). The host allowlist
is therefore **not** a security boundary for a request that never happens: it keeps a link to a
mirror, a shortener or an unrelated site out of the field a staff member is about to open by hand.

### 3. The code is stored in the clear, deliberately

A hash was considered and rejected. The code is **not a credential**: it grants nothing, authorises
nothing and identifies nobody. It has exactly one job — to be visible in two places at once, a
public profile and a staff screen — and a staff member comparing it by eye must be able to read
ours. Hashing it would mean typing what the profile shows into a form that hashes it, which moves
the comparison into a text box, shows the staff member nothing, and makes a typo indistinguishable
from a mismatch. Instead the exposure is bounded: 72 hours, single claim, replaced by any reissue,
dropped from the row the moment the claim settles, never written to the event log, and unguessable
(27⁸ ≈ 2.8 × 10¹¹ from `secrets`, in an alphabet with no ambiguous glyph).

### 4. It clears `identity_not_verified` through the field that blocker already read

There is **no second notion of "verified"**. `account_control_services.verify` calls
`rights.services.verify_identity`, which sets `RightsClaimant.identity_status` — the one field
`rights/gate.py` reads. Consequences of that choice:

- the older admin path (ADR 0034: register extract, company email, "other") **stays**. An
  organisation verified that way reads `staff_legacy` in this flow's status, and a database check
  constraint refuses `staff_legacy` as a *challenge's* method, so no real check can ever be
  mis-attributed to it;
- revoking a verification revokes the organisation's identity **only when no other verified account
  is left**: two accounts proved and one withdrawn does not unmake the other;
- identity says **who is asserting**, never what they hold. Nothing here touches a rights record.

**Where no platform checked anything, the code establishes nothing** (operator review,
2026-09-19). The whole argument of this ADR is that the platform checked identity and age before
the creator could earn money. Two entries in the list have no platform behind them — the creator's own
website, and "another platform" — so a code in such a page's bio proves only that somebody controls
that page. For those two: **no code is issued at all**, "I have placed it" is refused
(`video_call_required`), and only `video_call` may settle them. It is enforced three times over: in
`account_control.Platform.checks_identity` (the list itself), in `account_control_services.verify`,
and in a **database check constraint** (`account_challenge_video_call_only_platforms`), so a future
caller cannot record a bio code as what established identity there. The customer screen says so
before they submit, and a test asserts the list and the rule cannot drift apart: a platform we name
a host for is one we believe checks its creators.

**Methods**: `bio_code`, `dm_code`, `video_call`. The first two need the code still to be live. The
third is the fallback for doubt and for studios — staff see a document on camera and record **only
that the check happened** (date, staff user, method); no image, no number, no birth date, and the
code is beside the point for it. On the claimant it is recorded as `IdentityMethod.VIDEO_CALL`, a
new value beside `ACCOUNT_CONTROL_TOKEN`.

### 5. The customer side

`/api/v1/app/verification` (`marceline/app/verification.py`): the status (the platform list, this
organisation's claims, the live code of an open one, how identity stands), `POST /accounts` to claim
an account, `POST /accounts/{chal_…}/placed` for "I have placed it". Codes, not sentences; public
ids only; a malformed, unknown or another organisation's id is one byte-identical 404; CSRF on every
write. With the switch off **every route is 404**, like the upload routes with cloud fingerprinting
off.

In the app (`/app/verification`, Next, ADR 0036): the heading, the standing, the code and the list
of claimed accounts are **server-rendered**; one island holds the two forms. German „du". The screen
says, before anything else, that we never ask for a document, a date of birth or anything about age —
the sentence a creator who has been asked for a passport elsewhere needs to read first. The claimed
address is printed as text and never as a link.

### 6. On the public host: bundle in, outbox out — exactly as ADR 0046 did it

- **the read** is a document in the signed bundle (`verification`, one per organisation, no detail
  family — like `rights`), rendered by calling the customer view as one of that organisation's own
  members, so the bytes production serves are the bytes development would have answered. It is in
  `customer_cache.CACHED_ROUTES` and `store.DOCUMENT_KEY`, and a test asserts a bundle carries no
  other organisation's claim or code;
- **the two writes** are outbox kinds (`verification_claim`, `verification_placed`, `delivery/0009`)
  answered **202**; development applies them with the same two service calls the endpoint makes
  (`customer_writes.py`). The **code is generated on development**, never on the public host: it has
  to exist where the staff member who compares it by eye will read it, and it reaches the creator
  with the next bundle (at most ADR 0028's 15 minutes, and a write expires the bundle at once);
- `verification_placed` carries the claim's public id and resolves it **on development**, so a
  foreign or unknown id is refused there with the same answer the endpoint gives — production holds
  no challenge rows and cannot judge it;
- the switch must be set on **both** machines. Off on production, the step is a 404 there however
  development is configured; a compose-env test pins the passthrough in both compose files.

### 7. The staff side

The admin (`rights/admin.py`, Tailscale-only like every staff surface): issue a challenge, reissue a
code, see the claimed profile as copyable text, record the outcome with a method and an optional
note, record that the code was not there, revoke a verification with a reason. Every outcome requires
the staff user and is attributed in the append-only log. **No note is ever required**, because a
required free-text box is where somebody would type the thing we promised not to keep.

### 8. Onboarding points at it

The onboarding checklist gains the step, and the invite completion screen names it as
what comes next — but only where the switch is on (`InviteStateOut.account_step_next`, a fact about
the deployment and never about the token, so it tells nobody anything about the invite they hold).
The rights screen's identity block links here instead of offering ADR 0034's bare token when the
step exists.

## Consequences

- **A pilot creator can clear `identity_not_verified` themselves**, in a minute, without sending us
  anything. That was the last step of the rights side that had no customer surface at all.
- **We hold less than before, not more**: no document, no birth date, and the note is optional. What
  we gain is a *recorded* check — which platform, which handle, which method, which staff user, when
  — where the old token had none of it.
- **The check is only as good as the platform's own.** That is stated in §"Context" on purpose: if a
  platform is ever shown not to check identity or age before payout, the assumption behind this
  ADR fails for that platform and its entry comes out of the list. The list is code, so that is a
  one-line change with a test.
- A claimed handle is customer data: `customer_data_retention` (ADR 0045) deletes it with the rights
  block, named explicitly rather than left to the organisation row's CASCADE, because one legal hold
  keeps that row.
- **The customer's own data export (ADR 0045) carries the claimed accounts** in its own
  `account_verification` section — platform, handle, the address they gave us, the standing, the
  method, the dates and the whole event log — and **no code field**: a settled claim has none, and a
  live one is on the screen they are reading anyway. The `documents` section still mirrors the
  customer API byte for byte, so their own live code appears there exactly as their own screen shows
  it.
- Two migrations: `marceline/0027` (the two tables, the trigger, `identity_method`'s new choice) and
  `delivery/0009` (two outbox kinds). Both additive. Deploy development before production, as
  ADR 0028 requires.
- The wording rules (`frontend/src/i18n/wording.test.ts`) forbid "verified" and „verifiziert" in a
  customer string, which shaped every sentence of this screen: a state says what our record holds —
  "our team has seen it", "nobody has looked yet", "withdrawn" — and never what follows in law.

## Alternatives rejected

- **A third-party KYC or video-ID provider.** Biometric processing of a special category of data,
  a provider contract, a
  per-check fee, and an EU transfer question — for a check we chose not to make. It would also be
  the one thing we tell creators we do not do.
- **Ask for an ID copy and delete it after the check.** The safest version of this option is "accept
  it, look, destroy it", which still means the bytes existed on our disk. Refused outright.
- **A birth-date field "so we can check".** ADR 0052's amendment already refuses this for the
  majority declaration (Art. 5(1)(c) GDPR). Adding one here would undo that decision through a
  different door.
- **Fetch the claimed profile and look for the code ourselves.** It is the obvious automation and it
  is exactly what must not exist: a URL a stranger wrote, fetched by our infrastructure, against a
  platform whose terms we have not read, with a bot user agent, past a login wall in most cases. A
  human opening a public page in their own browser is the whole design.
- **Store the code hashed.** §3.
- **Keep ADR 0034's bare token and only add a screen.** It has no platform, no expiry, no record of
  what was seen and no way to reissue; "tell us where" asked the creator to use a channel this
  product does not have. The token stays in the model for now (it is what the older path used) and
  the screen offers this flow instead where the switch is on.
