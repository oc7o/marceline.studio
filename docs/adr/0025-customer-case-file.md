# 0025 — The customer case file: stored observations, derived steps, named absences

Status: accepted (2026-09-16)

Implements §3 of `docs/customer-ui-plan.md` (issue #32) on top of ADR 0021 (organisations and the
404 rule) and ADR 0023 (customer login). Backend and API only; the SPA is built in parallel.

## Context

The operator's direction is that the customer sees a **process**, with full transparency, and is
offered options rather than having decisions made silently: "a leak is not a database row, it is a
case you can follow". The plan's case file wants a status timeline (found → verified → notice
prepared → sent → acknowledged → removed → re-checked), what we know about where the copy sits, and
a removals log — and it defines **handled** as "removed *and* not re-uploaded anywhere", which is
the strongest of the three options the operator was offered.

Three facts about today's data decide the shape of this change:

- `ReferenceMatch`, `ReferenceLeak` and `Page` can answer *what* is true now, but not *since when*.
  `Page.availability` says a page is gone; `availability_checked_at` is the **last** check, and it
  keeps moving. Nothing records the transition.
- **Nothing prepares or sends a takedown notice.** Notice export is #33; acting on a customer's
  behalf is not decided (#46). Three of the seven steps therefore cannot be
  derived from anything, honestly or otherwise.
- **Hoster metadata, jurisdiction and cooperation history are not built at all.** They are the most
  valuable part of the plan's case file and the easiest to fake convincingly.

## Decision

### One case per original, many findings

`matching.LeakCase` (one per `Reference`) and `matching.CaseFinding` (one per page of it). A
finding is one URL; a re-upload elsewhere is a **new finding on the same case**, and a case that had
gone quiet **reopens** (`reopened_at`, `reopen_count`) instead of a second case being opened. That
is the "one case, many URLs" shape `index.SafetyIncident` already uses for hotline reports (#66),
reused as a shape rather than as a table: incidents are a safety record about pages a customer may
never see, so they can carry nothing for this timeline.

`LeakCase.state` is `open` or `removed_watching`. There is no closed state, because the operator's
definition of handled has a second half nothing can ever fully prove: a removals log is living
state, not an archive.

### The tables store observations only

What is derivable is derived at read time and not duplicated. What is stored is exactly what no
other table records — when a transition was **first** seen: `discovered_at` (from the match's or
leak's own `created_at`), `verified_at` (the decision row's `updated_at`, which is the only date a
decision has), `removed_at` (the first check that found the page gone) and `reappeared_at` (that
URL is live again; set instead of clearing `removed_at`, so an entry can reopen rather than quietly
vanish). A stamp is written once and never moves.

Reconciliation lives in `marceline/app/cases.py` (`sync_cases`), is idempotent, and runs **on read**
for the requesting organisation's own originals only. A case must reflect the crawler's latest
recheck and nothing else writes these tables yet; a scheduler job can later call the same function
without any change to the read model. The logic sits in the project package, not in `matching`,
because it needs the customer safety filter (`marceline/app/safety.py`) and `matching` must not
import `marceline` — the same reason `marceline/safety_incidents.py` exists.

### The timeline is codes and dates, never prose

The product is **bilingual from the start** (plan §8), so the API ships no sentence a customer could
read: every step is `{step, status, at, reason, blocked_by_issue}` with `status` one of `done` /
`pending` / `unavailable`. The UI owns the words, in the customer's language.

| Step | Derived from | Today |
|---|---|---|
| `found` | earliest `CaseFinding.discovered_at` | done as soon as anything is found |
| `verified` | earliest `verified_at` (a human decision, or an auto decision a reviewer can reverse) | pending while only matching has spoken |
| `notice_prepared` | — | **unavailable**, `capability_not_built`, #33 |
| `notice_sent` | — | **unavailable**, `legal_review_pending`, #46 |
| `notice_acknowledged` | — | **unavailable**, `prerequisite_unavailable`, #46 |
| `removed` | `LeakCase.removed_at`, set when **every** finding is gone | one URL of several coming down is a count, not this step |
| `rechecked` | latest `Page.availability_checked_at` of any finding | pending until the crawler has checked once |

A step that cannot be known says so with a reason code. Inventing a date, or hiding the gap by
leaving the step out, would both read as "this happened".

### Absences are named, not mocked up

`not_available` on the case detail lists, as codes: `hoster_metadata`, `jurisdiction`,
`cooperation_history`, `notice_preparation`, `notice_delivery`, `reupload_watch_coverage`. No empty
object pretends to be a hoster record, and no percentage or provider name is guessed. The last entry
is deliberate: "no re-upload anywhere" is only ever as true as our site coverage, there is no
per-original standing watch yet, and whether a customer sees the site list is an open product
question (plan §8, question 1) — so the case file says "not known" rather than implying omniscience.

### The removals log lists only confirmed removals

An entry needs both `verified_at` and `removed_at`: a page nobody confirmed as this original may
have vanished for any reason, and counting it as a takedown would claim work we cannot prove.
`handled` is true only when that URL is still gone **and** nothing else of the same original is up,
which is the operator's definition with both halves intact.

### Tenancy and safety are inherited, not re-implemented

Every view starts at `ownership.owned_references`; a foreign case answers 404 with a body
byte-identical to an unknown id, asserted as bytes. Findings come from `originals.findings_for`, so
a safety-flagged page is never a finding, never a removal and never a count — and if a page becomes
flagged later, reconciliation **deletes** what it had written, including the case with its last
finding: an empty case carrying `reopen_count = 2` would itself say something is being hidden. No
schema in `marceline/app` has a media field (#53); the absence is the guarantee.

## Consequences

- **A migration ships with this change** (`matching/0017_leakcase_casefinding_and_more`). Apply it
  from the main checkout with **`make migrate`, and restart the workers** (`make workers`) — a
  stale container writing against the old schema is how 997 pages were lost on 2026-09-15.
- `backend/openapi.json` is regenerated here; `frontend/src/api/schema.d.ts` needs
  `make frontend-api` from the main checkout, which a worktree has no `node_modules` for.
- **A `GET` writes rows.** It is bounded (the caller's own originals), idempotent and transactional,
  but it means the customer API is not read-only at the database level any more. A scheduler job
  calling `sync_cases` would make the writes on read almost always no-ops; it is not built yet.
- Case state is only as fresh as the last recheck. `crawl_recheck` skips removed pages by default,
  so a re-upload at the *same* URL is noticed only when something re-queues it — the honest
  counterpart of `reupload_watch_coverage` being listed as not known.
- No admin screens for cases yet: the operator's window into this is the review API and the tables.

## Alternatives rejected

- **Deriving removal dates from `availability_checked_at`.** It is the *last* check, not the first
  one that found the page gone, and it moves. Presenting it as "removed on" would be a fabricated
  date on the most quotable line of the product.
- **A case per finding.** Then a re-upload is a new case, the removals log becomes an archive, and
  "removed and not re-uploaded anywhere" cannot be expressed at all.
- **Reusing `index.SafetyIncident` as the case table.** Its subject is content a customer must never
  see, it holds hotline reporting metadata, and it has a retention clock that deletes records. Only
  the shape transfers.
- **Prose status text in the payload** ("notice prepared, awaiting legal review"). Untranslatable,
  and it puts product language in the API where two languages have to agree.
- **Empty hoster / jurisdiction objects** so the UI "has somewhere to render". A plausible
  placeholder later reads as data; a named absence cannot.
- **Omitting the unavailable steps entirely.** A seven-step timeline showing four steps invites the
  reader to assume the rest happened silently.
- **A 403 for another organisation's case.** It confirms the case exists (ADR 0021).

## Amendment 2026-09-16 (ADR 0027)

Hoster metadata and jurisdiction are built: every finding carries a `hosting` section, and
`not_available` no longer lists `hoster_metadata` or `jurisdiction`. `cooperation_history` stays
listed (`prerequisite_unavailable`, #33) — customers see a takedown-chance band per provider, which
is `unknown` until notices exist. Content hosts come from media URLs reduced to hostnames in
`marceline/hosting.py`; `marceline/app` still reads no media and has no media field.

## Amendment 2026-09-17: confirmed copies only, our step dated to the day

Operator decision: customers must not learn what we are still looking at, or how fast we review.

- **Unconfirmed findings do not exist for a customer.** Every customer view reads through
  `originals.confirmed_findings_for`: an unconfirmed page is not listed, not counted
  (`FindingCountsOut` is `{confirmed, removed}`, no `total` / `reviewing`), has no case, no
  removal and no URL in any response. An original whose only copy is unconfirmed answers like an
  original nothing was found of (asserted in `test_app_cases.py`). There is no page-keyed customer
  endpoint, so "an unconfirmed page id is not found" holds by absence; the SPA renders a page id
  that is not among an original's findings as "not found".
- **Found and verified are one step, `confirmed`, carried as a date** (`CaseStepOut.on`, no time).
  `discovered_at`, `verified_at`, `first_seen` and `opened_at` left the customer schema; the case
  carries `opened_on` / `reopened_on`, findings and removals `confirmed_on`. No pair of customer
  fields measures review latency. External steps (notice prepared, sent, acknowledged, removed,
  re-checked) keep their moments. The stored stamps are unchanged.
- `findings_for` (confirmed or not) stays for internal callers — hoster lookup priority in
  `marceline/hosting.py`.

## Amendment 2026-09-17: one case per video, not per reference (ADR 0031)

A re-fingerprinted video is several `Reference` rows with one sha256 (ADR 0022), and the test
organisation saw 41 originals for 39 videos. Customer views now read `ownership.owned_originals`:
the references of one file are one original under the representative's id (the newest reference
on a current pipeline), findings are merged per page (`originals.confirmed_findings_of`: confirmed
on any member counts, earliest observation wins), and a member's id is 404 byte-identical to an
unknown one. `LeakCase` stays one-to-one with `Reference` and lives on the representative:
`cases._case_of` moves it there when a newer reference appears — same row, every stamp kept — and
merges the cases two members grew before grouping (every page once, earliest `discovered_at` and
`verified_at`). The per-original sync lock is keyed by the file's sha256. With
`CASE_SYNC_ON_READ` off, a case still on an older member is read from there until `sync_cases`
moves it.
