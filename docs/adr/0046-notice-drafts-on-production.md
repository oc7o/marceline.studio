# 0046 — Notice drafts on the public host: bundle, outbox, and a download that records itself

Status: proposed (2026-09-18). Issue #86. Extends ADR 0028 (the read-through cache and its
customer write path) to the notice flow of ADR 0035 and its amendments; follows the precedent ADR
0034's rights model set there (bundle export plus outbox item types, with the sensitive half
staying on development). Does not touch ADR 0035's central rule: **Marceline never transmits a
notice**.

## Context

A pilot customer signs in on the public host. Everything they need to act on a finding is there —
their originals, their findings, their case files, their rights record — except the one screen the
product exists for: "Prepare notice draft". `/api/v1/app/notices` sat in
`public_surface.ROLE_ONLY_PREFIXES["dev"]`, and correctly so:

- **no read was in a bundle.** `marceline/export.py`'s documents were `originals`, `findings`,
  `cases`, `removals` and `rights`. Production's database holds accounts, sessions and the cache and
  nothing else, so an un-cached notice read would have answered from empty tables — `[]`, which
  reads to a customer as "no drafts", the one answer ADR 0028 forbids;
- **every step of the flow is a write**, and a customer write on production may only enter the
  outbox (ADR 0028 amendment "The production write path");
- **the gate's inputs do not exist there.** `notices/gate.py` reads `matching.ReferenceMatch` rows,
  `matching.copy_extent`, `index.Page.availability`, the rights record and the provider directory.
  None of those tables are on the public host, and putting them there would be alternative C of
  ADR 0028 — a second implementation of the most sensitive code we have.

Simply removing the prefix would therefore have produced a screen that lists nothing, offers
candidates for no finding, and accepts approvals nothing applies.

## Decision

### 1. The candidates are computed on development and exported, like every other read

`GET /notices/candidates/{original_id}` is the gate's own answer: per confirmed finding, the
blocker codes, the extent, the suggested providers and channels. It is computed where its inputs
are — on development — and travels in the bundle as a document per owned original. Production
matches the request path to that document and returns it. No gate logic, no directory, no match row
and no rights record moves to the public host.

The cost is freshness: a candidate list is up to 15 minutes old (ADR 0028's TTL), and a write
expires the bundle, so it is refetched at once after anything the customer did. That is the same
trade every other customer screen already makes, and the gate is re-run on development when the
draft is actually created — the exported list is what to *show*, never what to *allow*.

### 2. Which reads enter the signed bundle

| Document | Per | What it is |
|---|---|---|
| `notices` | organisation | the draft list, safety filter applied (`services.visible`) |
| `notices/{notice_id}` | visible draft | one draft with its findings, confirmations, required confirmations and frozen text |
| `notices/*` | organisation | the byte-identical 404 an unknown, foreign or malformed draft id gets |
| `notices/candidates/{original_id}` | owned original | the gate's candidates, above |
| `notices/candidates/*` | organisation | that family's 404 |
| `notices/{notice_id}/export/{txt,eml,annex}` | visible draft × format | the download, below |

They are rendered the way ADR 0028 renders everything: `marceline/export.py` resolves the customer
path and calls the very view a signed-in member reaches, so the bytes production serves are the
bytes development would have answered. There is no second serializer.

**A draft the safety filter hides has no documents at all.** `visible_draft_ids` is
`services.visible` applied to the list, so a draft whose reported page became safety-flagged
disappears from the bundle's keys, and its download keys with it. The pages a notice document names
are collected into the bundle's `page_ids` like any other (`NoticeFindingOut.page_id` keeps the
field name the collector reads), so the `delete` purge a safety block sends (ADR 0028 step 4)
finds and **deletes** the bundle that mentions it: on production a safety-hidden draft is the same
byte-identical 404 as an unknown one, and a stale copy can never be served again.

### 3. The three download files travel as rendered bytes, not as a re-render on production

The choice was between rendering on production from bundle data with the pure module
(`notices/export.py` deliberately imports no Django and no transport) and exporting the rendered
bytes. **The bytes travel.** Reasons, in order:

- **byte identity is by construction.** `txt` must be `approved_text` byte for byte, because the
  draft froze that text's sha256 at approval and the annex prints the checksum. Re-rendering on a
  second machine puts a second encoder between the customer and a checksum they are shown;
- **the annex needs facts the customer schema does not carry** — each finding's first-seen date and
  last-checked timestamp, the legitimate source URL. Re-rendering would mean widening the customer
  API for every customer to feed one machine's renderer, or shipping a second document only
  production reads;
- **it costs nothing.** A notice is a few kilobytes of text. Three files per approved draft is the
  smallest part of a bundle that already carries every case file.

The checksum is verifiable on production either way: `notices/{id}` carries `approved_text`, the
`txt` download carries the same bytes, and the annex prints the sha256 the draft froze. A test
asserts that production's `txt` equals the approved text of the detail document.

A draft that is **not exportable** — not yet approved, or withdrawn or corrected — has its document
too, carrying the 409 (`not_approved` / `already_closed`) development would answer. That is why
`delivery.store.DocumentIn.status` now admits 409 beside 200 and 404: a bundle carries a read's
answer, and for this read the answer is sometimes a refusal. It never carries a 5xx.

### 4. The download is served by the view, because recording it is a write

Every other cached read is served by `CustomerCacheMiddleware`. The download is not, and this is
the one deliberate exception in the design. ADR 0035's amendment requires each download to append
one `exported` `NoticeEvent` naming format, language, variant and the approved text's sha256 —
that log is append-only and lives on development. So a download on production is a read of the
bundle **plus** a write, and a write does not belong in a middleware.

`marceline/app/notices.py` therefore handles it on both machines:

- on development it formats through `services.export_notice` (format + log), as before;
- on production `_export_from_cache` takes the document out of the bundle
  (`store.document_for`, with `notices/*` as the fallback for an unknown id or an unknown format),
  answers it with the same headers development sets — `Content-Disposition` from the one
  `export.filename_for`, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store` —
  and, only for a 200, enqueues one `notice_exported` outbox item. Development appends the event
  when it applies it. Nothing servable is the same 503 `data_temporarily_unavailable` the
  middleware gives, from the same helper.
- **rendering a bundle is not a download.** `export.render` marks the synthetic request
  (`bundle_render`), and the view then formats through the new `services.export_payload` — the same
  formatter without the event — and never reads the cache. Otherwise every cache refetch would
  invent an export the customer never made, and a process in the `prod` role rendering a bundle
  would read its own cache in a loop.

`test_public_surface.py`'s classification gains a fourth, named case for exactly this route
(`CACHE_READING_VIEWS`), with the reason written down. It is not an exemption: the route is still
answered from the signed bundle, and a test proves it.

### 5. Which writes become outbox item types

| Endpoint | Kind | Development applies it with |
|---|---|---|
| `POST /notices` | `notice_create` | `app.notices.create_notice` — the function the endpoint itself calls |
| `POST /notices/{id}/approve` | `notice_approve` | `services.approve` |
| `POST /notices/{id}/mark-sent` | `notice_mark_sent` | `services.mark_sent` |
| `POST /notices/{id}/response` | `notice_response` | `services.record_provider_response` |
| `POST /notices/{id}/withdraw` | `notice_withdraw` | `services.withdraw` |
| `POST /notices/{id}/correct` | `notice_correct` | `services.correct` |
| `GET /notices/{id}/export/{fmt}` (the audit event only) | `notice_exported` | `services.export_notice` |
| `POST /notices/sandbox-send/{id}` | **none** | development only, never on production |

Production checks only what it can check by itself — session, CSRF, membership, the
per-organisation rate limit (`upload_throttle.Kind.NOTICE_WRITE`, 60/hour) and the request schema —
and answers **202 `NoticeWriteAcceptedOut`**. It cannot judge a notice write at all: the gate, the
safety state, the draft's status and the provider directory are all on development.

**A write accepted on production is judged again on development, and may be refused.** Every
handler resolves the draft through `app.notices.draft_for`, the same chokepoint the endpoints use,
so the tenancy rule and the safety filter apply there: a draft whose page became safety-flagged
between acceptance and application is refused `notice_not_found` — the answer an unknown id gets,
never a hint that something is hidden. `create_notice` and `services.approve` re-run the notice
gate; `services.mark_sent`, `withdraw` and `correct` re-run the state machine. The refusal reaches
the customer (below).

### 6. Idempotency and ordering

- **Ordering per organisation is ADR 0028's, unchanged**: `delivery.inbox.run_once` applies pulled
  items in acceptance order, once per item (`InboxItem` in the applying transaction), and a failing
  item holds back that organisation's later writes for the rest of the run. That matters here more
  than for rights: create must precede approve, approve must precede mark-sent.
- **Idempotency key**: `OutboxItem.dedupe_key`, which already collapses a still-pending duplicate.
  A per-draft action keys on the draft's public id, so a double click is one write; `notice_create`
  keys on `original_id|hostname|channel_kind`, the draft's own canonical identity (ADR 0035:
  canonical per organisation, recipient provider and channel). `notice_exported` has **no** key:
  each download is its own audit row.
- After `MAX_APPLY_ATTEMPTS` (3) an item is quarantined with the exception's class name, as today.

### 7. What the customer sees while a write is pending

The 202 carries `pending: true` and a submission id, and both apps render one factual line under
the step — "Received. It will show here shortly." — never a claim that the step happened. The three
refusals production can give *before* accepting (outbox off, development not pulling, rate limit)
share one sentence: nothing was saved, try again shortly. Which of our plumbing reasons it was is
not something a customer can act on.

`GET /api/v1/app/notices/submissions` — a **production-local** read, like
`fingerprints/submissions`: this organisation's notice writes with kind, `processing | done |
refused | failed`, the outcome's code and the draft's public id once it is known. Empty on
development, where a write is applied at once. It is how a customer would learn of a refusal that
only development can decide — a draft whose page was hidden between acceptance and application is
refused `notice_not_found`, and the screen must say so rather than silently stay unapproved.

**The endpoint is built and tested; no screen reads it yet, and that is a budget decision, not an
oversight.** `/app`'s first load sits at the 150 KB phase 1 ceiling (ADR 0028 d.1) and the whole
language file ships inside it, so a block with a heading and a line per outcome did not fit: the
measured first load went to 150.21 KB with it and 149.9x KB without. What fits — and what the
customer needs first — is the pending line plus the `not_saved` and `notice_not_found` sentences.
The block belongs in the Next app on the day the SPA stops shipping the words to that ceiling
(ADR 0036's cut-over); `isUnsettled`, `codeKey`, `useNoticeSubmissions` and `readNoticeSubmissions`
are in place for it and say so where they live. Until then a refusal after acceptance is visible to
staff (the admin's outbox items) and not to the customer, which is the one honest gap this slice
leaves.

### 8. The sandbox send stays development-only, and moves path to say so

`services.sandbox_send` is the only code path in the product that may hand a draft to a mail
transport. `ROLE_ONLY_PREFIXES` is a **prefix** allowlist, and `/notices/{id}/sandbox-send` puts
the variable part first, so no prefix can name it. Rather than teach the allowlist suffix matching
— a weaker primitive in the most security-sensitive file we have — the route moved to
**`POST /api/v1/app/notices/sandbox-send/{notice_id}`**, and that prefix is in
`ROLE_ONLY_PREFIXES["dev"]`. On production the path 404s before anything routes.

**Still no sending, ever.** A test walks every read and every write this change added, applies
every outbox item, and asserts the mail outbox is empty.

### 9. Edge and allowlists

- **Caddy: no change.** Notice paths are under `/api/v1/app/*`, which Caddy already forwards, and
  the bodies are small — the 1 MB cap covers them with room; the 16 MiB exception stays exactly
  `POST /api/v1/app/fingerprints`. `make prod-config`, `make prod-surface-check` and
  `infra/prod/check_config.py` need no new expectation, because none of them enumerates a customer
  route below the `/api/v1/app/*` prefix.
- **Django**: `notices` leaves `ROLE_ONLY_PREFIXES["dev"]`; `notices/sandbox-send` joins it.
  Invoices (ADR 0032) and rights proofs (ADR 0034) stay where they were.
- `no-store` on every answer is unchanged and now covers the new routes by construction
  (`marceline/no_store.py` matches the customer prefix; the download sets its own, identical value).

## Consequences

- **A pilot customer on the public host can do the whole flow**: see the candidates for a record,
  prepare a draft, confirm the declarations, approve, download the three files, mark it sent,
  record the provider's answer, withdraw, correct. Each write is answered "received" and appears a
  minute later; each read is at most 15 minutes old, and a write expires the bundle at once.
- **What they cannot yet see** is a refusal that arrives after acceptance (§7): the endpoint for it
  exists, the screen waits for the SPA's retirement. In practice the draft simply stays as it was,
  and the operator sees the refused outbox item.
- **Two migrations**: `delivery/0006` (seven outbox kinds, and `original_id`'s help text, which now
  also holds a draft's public id) and `marceline/0017` (the `notice_write` throttle kind). Both are
  additive `AlterField`s on choices.
- **Deploy development before production**, as ADR 0028 already requires: a kind production can
  accept and development cannot apply quarantines every time. A test asserts every kind has a
  handler.
- The bundle grows by roughly 4 documents per draft. For a pilot customer with a handful of drafts
  that is single-digit kilobytes; if a customer ever has hundreds of drafts, the download documents
  are the part to bound first (only the open ones, say), not the details.
- The rendered notice wording is untouched. This change moves bytes; it writes no sentence a
  customer sends.
- ADR 0035's consequence "this feature's write endpoints stay development-only … when a production
  sending/outbox path is built, it is a new ADR" is discharged by this ADR — for *drafting*. Real
  transmission by Marceline remains out of scope and blocked on #46.

## Alternatives rejected

- **Render the downloads on production from the cached detail document.** `notices/export.py` is
  pure and would run there, but the annex needs per-finding dates the customer schema does not
  carry, so it would mean widening the public API to feed a renderer, or shipping a parallel
  document. And a second encoder between the customer and the checksum they are shown is exactly
  the drift ADR 0028's "the bundle is the customer API itself" exists to prevent.
- **Serve the download from the cache middleware and enqueue the audit item there.** Fewer lines,
  but a middleware that writes to the outbox decides what a write *means*, which is the one thing
  `delivery` is not allowed to do. It also loses the `Content-Disposition`, which would have had to
  be rebuilt from the path in the middleware.
- **Compute the candidates on production.** Alternative C of ADR 0028, applied to the notice gate:
  it would need match rows, copy extent, the rights record, the page's availability and the
  provider directory on the public host, and a second implementation of the gate. The gate is the
  thing that decides whether a customer may accuse someone.
- **A `ROLE_ONLY_SUFFIXES` companion for the sandbox send.** Five lines, and it would have kept the
  path. Rejected: suffix matching on a URL is a weaker primitive than prefix matching, in the file
  whose whole job is to be obviously right. Moving one route the operator's sandbox organisation
  uses is cheaper than making the wall cleverer.
- **Answer notice writes 503 on production until they can be applied there.** Honest, but it leaves
  the pilot customer with a read-only screen that shows drafts they cannot act on — the state issue
  #86 was opened to end.

## Amendment (2026-09-18): the §7 gap is closed (issue #86)

Status: accepted. `GET /api/v1/app/notices/submissions` now has a screen: `NoticeSubmissionsBlock`,
one component per app, same name, on `/app/notices` and `/app/originals/{id}/notices`. §7's "no
screen reads it yet" is superseded by this amendment; the paragraph above is left as the record of
why it shipped in two steps.

**What closed the budget gap.** Issue #99 (2026-09-18, the same day) put the customer words behind
per-screen loaders instead of one shared language file, which is what §7 named as the blocker: the
SPA's first load dropped to 142.65 KB of the 150 KB ceiling and a `notices` block's own words now
cost only the notices routes, never every visitor. That is the room this block spends.

**What the customer sees per state**, one line each: pending ("received, being applied" — the
existing line, unchanged), refused with a sentence they can act on, and applied — which needs no
line at all, because the draft itself, open on the very same list, is the record of success; the
moment a write's status flips from `processing` to `done`, the next read of the page simply omits
it. There is no separate fade-out or timer.

**Refusal codes are narrowed, not just relayed.** `finding_not_eligible` /
`finding_no_longer_eligible` fold every blocker of every page in the refusal into one code
(`notices/CLAUDE.md`), which by itself says only "something changed" — true, but nothing a customer
can act on days after the fact with no live screen in front of them. `customer_writes.py
_submission_code_for` classifies the blockers a refused create/approve actually carried, from the
very same codes the live create/approve screen already shows inline: all on the rights side becomes
`finding_rights_blocked` ("your rights record needs attention — check it on the rights page"), all
about the page's own availability becomes `finding_unavailable` ("nothing more for you to do here"),
and a mixed or otherwise-shaped refusal keeps the generic code, whose sentence ("reload and check
the list below it") is still true either way. No schema change: `NoticeSubmissionOut.code` was
already a plain string, so this is a backend behaviour change only, covered by three new tests in
`test_notices_on_production.py`. Every other refusal a customer can actually cause
(`notice_not_found`, `unknown_page`, `not_draft`, `already_closed`, `confirmation_incomplete`, …)
reuses the exact sentence the live screen already has for that code; anything without words of its
own (`no_organisation`, `invalid_payload`, and the handful of codes production's own schema
validation already rules out before a write is even queued) reads one neutral line with the support
contact, never the code.

**Two components, one no client-side polling.** The SPA's is an ordinary hook-driven component on
`useNoticeSubmissions()` (already built for ADR 0046, polls every 15 s while something is
`processing`, exactly like the existing upload-submissions block). The Next app's is a plain
**server** component — zero client bytes — fed the read as a page prop; instead of a polling timer
it offers a plain, no-JS `<a>` link back to the same page while something is still pending, because
the operator's own guidance for this route was "no client JS unless polling is truly needed" and a
route this close to its own budget should not spend client JS on a feature that can be a link.

**Measured first loads with the block built** (check-runner, 2026-09-18, `make remote-check` on
branch `notice-submissions-block`): the SPA's first-load total is unaffected (142.70 KB of 150 KB —
the block is one more per-route addition, not first-load weight); its own two routes grew inside
their budget, `/app/notices` to 7.97 KB of 16 (3.70 KB of that its own words) and
`/app/originals/{id}/notices` to 11.95 KB of 16 (also 3.70 KB words, up from the 10.96 KB issue #99
measured with nothing on the route yet). On the Next app the server component cost exactly nothing:
`/app/notices` stayed at 139.41 KB first load, 5.31 KB of its own chunks — identical to a plain
route; `/app/originals/{id}/notices` (the island route) moved from 145.55 KB/11.55 KB to
145.73 KB/11.63 KB, all of it the shared `@shared/app/notices` module's own small growth, not the
block itself. Both apps stay inside their 150 KB first-load and 16 KB per-route budgets.

Full passage, the two components and the archived pre-amendment CLAUDE.md text:
`docs/conventions/customer-ui.md` ("Notice submissions block").
