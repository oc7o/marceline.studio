# 0045 — A customer's data is deleted 30 days after their pilot ends, and per original when they lose it

Status: **accepted** (2026-09-18). Built and **off by default**
(`CUSTOMER_DATA_DELETION_ENABLED`). Issue #85. Builds on ADR 0031 (ownership is a column),
ADR 0025 (the customer case file), ADR 0034 (rights records, legal hold, `rights_housekeeping`),
ADR 0035 (notice drafts and their append-only events), ADR 0032 (invoices and the trigger that
protects them), ADR 0028 (the production read-through cache and the accounts document) and
ADR 0029's retention amendment, whose `page_retention` is the house pattern for a deleting command.

## Context

Operator decision of 2026-09-18, twice:

1. **When a pilot ends, the customer is offered an export and their data is deleted 30 days
   later.** Invoices and cancellations keep their own statutory retention (ADR 0032); rights
   records on legal hold are excluded (ADR 0034).
2. **The same must exist for a single original** — *"if they switch the owner we need to be able to
   delete single resources as well."*

Nothing enforced either today. The scheduler knows `page_retention`, `invoice_retention` and
`rights_housekeeping`, and no job that ends an organisation's data; the 30 days were a calendar
entry.

Storage limitation (Art. 5 (1) (e) GDPR) is what makes a fixed period necessary. Two points are
decided here rather than inherited:

- the period is a setting, not a constant compiled into the logic;
- the end of a pilot is **a date a human sets**, not something inferred automatically.

Accounting documents keep their own statutory retention:
ADR 0032's amendment of 2026-09-17 established eight years from the end of the year of issue, and
the billing trigger enforces exactly that. Nothing in this feature touches an invoice.

### What an owner switch leaves behind today

`ownership.reassign` (ADR 0031) moves `marceline.OriginalOwnership.organisation` and nothing else.
Reading the code for what that leaves:

| Row | Keyed by | Today, after a switch |
|---|---|---|
| `matching.Reference`, `ReferenceFrameHash`, `ReferenceMatch` | the reference | **travel** to the new owner (this is the point of a reassignment) |
| `matching.LeakCase`, `CaseFinding` (ADR 0025) | the reference | **travel** — the new owner immediately sees the previous owner's `opened_at`, `reopen_count` and every per-finding `verified_at`/`removed_at` |
| `marceline.NoticeDraft` + findings, confirmations, events (ADR 0035) | organisation × sha256 | **stay with the previous owner**, who keeps seeing drafts, reported URLs and their own confirmations for a work that is now somebody else's |
| `marceline.WorkRights` + transitions, `ProofDocument` (ADR 0034) | organisation × sha256 | stay with the previous owner |
| `marceline.OriginalPublicId` | organisation × sha256 | stays (documented: it answers 404 afterwards) |
| `marceline.RefusedClaim` | organisation, sha256 | stays |
| `marceline.RightsConfirmation` | the reference | **travels**, while naming the *previous* organisation |

So both halves of the operator's sentence are violated in opposite directions: the new owner
inherits a case history, and the previous owner keeps a live view of an original they no longer
have. Neither is fixed by the organisation-wide clock — the previous owner is still a customer.

## Decision

Two scopes, one clock, one deleting job.

### The clock is a date a human set, never an inference

- **Organisation scope**: `marceline.OrganisationEnding`, one row per organisation, with
  `ended_at` — set deliberately by staff in the admin (confirmation tick) or with
  `manage.py end_pilot`. No job derives it from the last login, the last invoice, an expired
  subscription or an empty account. An account nobody marked is never deleted.
- **Original scope**: `marceline.OriginalRemoval`, one row per **organisation × sha256** — the same
  key the rights records and the public id use, because it has to outlive the ownership row it is
  usually created by. `removed_at` is its clock.

Deletion happens at `clock + CUSTOMER_DATA_RETENTION_DAYS` (default **30**). The window is a
setting, so the period can change without a code change.

### On an owner switch, one thing cannot wait and everything else takes the window

`ownership.reassign` — and therefore `manage.py assign_originals` and the admin's *Original
ownerships* — now does two extra things for every original whose owner actually changes (a move to
another organisation, and `--unassign` alike):

1. **The case file does not travel: it is deleted at once.** `matching.LeakCase` and its
   `CaseFinding` rows for every reference of that file are deleted inside the same transaction as
   the move. This is the one thing that cannot wait for a window, because the moment the move
   commits the *new* owner's customer view reads those rows. The next `sync_cases` rebuilds a case
   for the new owner from the match rows as they stand. Being exact about what that means: the
   rebuilt case's `discovered_at` comes from the match rows, which are our own observations about
   the work and stay; what does not survive the move is the previous owner's case row itself — its
   `opened_at`, its reopen history, and every per-finding `verified_at`, `removed_at` and
   `reappeared_at` stamp. That is the customer-specific part, and it is what the operator's "the
   new owner must never inherit the previous owner's case history" names.
2. **The previous owner's per-original data is marked for removal**, `removed_at = now`, reason
   `owner_changed`. It is then deleted by the ordinary job after the window — it is scoped to the
   previous organisation and no other organisation can see it, so there is no reason to take it
   away before the customer has had the chance to export.

`assign_originals --dry-run` and the admin's confirmation text say both of these **before**
anything happens, so the operator who wants the previous owner to keep the case history takes the
export first. A move is rare and operator-driven; an automatic export at that moment would be a
file written by a side effect, which this project does not do.

### What each scope deletes

**Organisation scope** (`OrganisationEnding` past its window), in this order, all in one
transaction per organisation:

1. notice drafts of the organisation, with their findings, confirmations and events (ADR 0035);
2. its rights records (ADR 0034): work records with their transitions and proofs (the vault file
   first, as `rights_housekeeping` does), agency mandates, catalogue schedules, licensed partners,
   the claimant row, and the organisation-level events — **except anything on `legal_hold`**;
3. refused claims, upload attempts, original public ids;
4. every original it owns: the `matching.Reference` rows, which take their frame hashes, their
   match rows, their case file and its findings, the ownership row and the upload's rights
   confirmation with them;
5. memberships, and the customer accounts behind them;
6. the organisation row itself.

**Original scope** (`OriginalRemoval` past its window), for that organisation and that sha256 only:
its notice drafts, its work rights record with transitions and proofs, its public id, its refused
claims, its rights confirmation — and the references of that file **that this organisation still
owns at deletion time**, with everything that cascades from them.

### What is never deleted by either scope

- **Pages, page media and page frame hashes, clusters, common frames, breadcrumbs, the index.**
  They are ours, not the customer's: we crawled them, and a customer leaving does not unmake what
  the index knows about a pirate site. Their own retention is ADR 0029's `page_retention`.
- **A file another organisation owns.** The reference set of an original scope is filtered by
  ownership *at deletion time*, so a file that moved on is untouched; an organisation-scope pass
  skips any file another organisation holds a reference of. Unassigned references of the same file
  are also left alone: they belong to nobody and are not the leaving customer's to take.
- **Invoices and cancellations.** ADR 0032 owns them; the billing trigger refuses deleting one
  before its eight years are up, and `Invoice.organisation` is `PROTECT`, so the organisation row
  survives its own deletion pass while an invoice names it. That is the intended outcome, and it is
  reported as kept, not worked around.
- **Rights records, proofs, mandates and schedules on `legal_hold`.** They keep the organisation
  row alive too (`PROTECT`), and the pass says so.
- **Safety incidents, their pages and their copied hashes** (ADR 0011/0018). They are a safety
  record about pages, not about a customer; nothing here reads or writes them.
- **Staff accounts.** A `is_staff`/`is_superuser` user who happens to hold a membership loses the
  membership and keeps the account.

### What the export contains

`manage.py export_customer_data --organisation NAME --out PATH` writes one JSON document
(`format: marceline.customer-export`, version 1). It reuses what exists and adds no dependency:

- the **customer's own API documents**, rendered through `marceline.export.render` — the same
  function ADR 0028's bundle uses, which resolves the customer path and calls the very view a
  signed-in member would reach. So `originals`, `findings`, `cases`, `removals`, `rights` and every
  `cases/{id}` in the export are byte-for-byte what the customer was being shown, including the
  page addresses they were already shown, and nothing they were not (safety-filtered, confirmed
  only, no media, no confidence);
- their originals' **metadata and fingerprints**: per reference the pipeline, sampling rate,
  digest, title and each frame's `frame_index`, `timestamp_s`, `dihedral`, `quality` and PDQ hash
  as hex — the frame shape of `matching.fingerprint.to_json`, so the hashes are recognisable to
  anyone who has seen the client's document. It is **not** a re-importable client document and does
  not claim to be: `file_size` and `image_count` are not stored on a `Reference`, and inventing
  them would be a lie in a file a customer keeps;
- their **notice drafts** (ADR 0035): each draft's snapshot fields, its reported URL snapshots, the
  exact confirmation texts they ticked and its event log. This is their own audit trail of what
  they asserted, and it is the part of a deletion that is hardest to reconstruct.

Hashes, ids, dates and codes only. No frame, no thumbnail, no poster, no pirated title of our
own making — every string about a page in there is one the customer was already shown.

**A customer-triggered export** plugs in as `GET /api/v1/app/export` in `marceline/app/`, calling
the same `export_document(organisation)` for `organisation_of(request.user)` and streaming it as an
attachment. It stays development-only (`ROLE_ONLY_PREFIXES["dev"]`, as rights and notices are)
until someone decides how a 100 MB document travels through the signed cache; production's copy of
a customer's data is the bundle, which holds none of the fingerprints.

### The audit record that survives the deletion

`marceline.DataDeletionRecord`: scope, the organisation's **primary key and public id as strings of
the moment**, the sha256 for an original scope, the marked, due and deleted timestamps, the window
in days, per-table counts, the codes of what was kept, and the staff account that ran it (empty for
the scheduler). No name, no username, no email, no URL, no title — nothing that identifies a person
or a work beyond a digest. It has **no foreign key to the organisation**, precisely so that it
survives it, and a trigger refuses every `UPDATE` and `DELETE` on it (the technique of
`billing/0002` and `marceline/0007`): a record of a deletion that could itself be deleted answers
nothing.

### Database-level protection, not only Python

The project's rule is that what may not be deleted is refused by the database
(`billing/0002` + `0004` for invoices, `NoticeFinding.page` `PROTECT` for the pages of a real
notice). This feature adds one guard of its own and relies on three that exist:

- **new**: a `BEFORE DELETE` trigger on `marceline_workrights`, `marceline_proofdocument`,
  `marceline_agencymandate` and `marceline_rightsschedule` that refuses to delete a row with
  `legal_hold = true` (SQLSTATE 23000 → `IntegrityError`). Until now the hold was a Python check in
  `rights/retention.py`; a legal hold that a raw `DELETE` or a future code path can step over is
  not a hold. Nothing in the codebase deletes a held row today, so the trigger changes no behaviour
  — it makes the existing behaviour enforceable.
- existing: the billing trigger for invoices, `Invoice.organisation` `PROTECT`, `WorkRights`/
  `ProofDocument`/`AgencyMandate`/`RightsSchedule`/`NoticeDraft`/`RightsClaimant` `PROTECT` on the
  organisation — which is what makes "the organisation row could not be deleted" an observable,
  reported fact rather than a silent partial deletion.

### The production side (ADR 0028)

Two things, both of which already exist:

- a **`delete` cache purge** naming the organisation is written in the same transaction as the
  deletion and delivered on commit, so production drops that organisation's cached bundles rather
  than serving them for up to 72 stale hours;
- the **accounts document** is a full replacement: `pull_accounts` deletes on production every user
  and organisation development no longer exports (`marceline/accounts_replica.py`), which ends the
  customer's sessions there within 15 minutes. Nothing new is needed, and nothing may be: production
  never deletes customer data on its own initiative.

An organisation the pass kept back (an invoice, a legal hold) keeps its login, which is correct:
their invoices are still theirs to fetch.

### Off until enabled

`CUSTOMER_DATA_DELETION_ENABLED` (default false) guards the scheduler job
`customer_data_retention`, which runs **last**, after `page_retention`, exactly like it: deletion
is irreversible, so the operator reads `manage.py customer_data_retention --dry-run` and turns it
on once by hand. `CUSTOMER_DATA_RETENTION_DAYS` (default 30) is the window. Both are passed
through `infra/compose.yaml`'s `scheduler` service and pinned by `test_compose_env.py`: a setting
the compose file names to nobody is silently empty forever, whatever `.env` says.

Marking is **not** guarded by the flag: marking an organisation as ended writes a date and deletes
nothing, and the mark is what the dry run reports on.

## Consequences

- The 30-day promise stops being a calendar entry: there are two marks, the export and the job.
- One migration adds three tables and two triggers (the deletion record's append-only rule and the
  legal-hold refusal). `make workers` after it, like every migration.
- `ownership.reassign` gains a side effect — the case-file reset and the removal mark. It is the
  single writer of ownership, so every path (CLI, admin, `seed_test_customer`) gets it; the dry run
  shows it first.
- A customer who deletes one original and then re-adds the same file inside the window keeps the
  pending mark: the mark is per organisation × sha256 and `remove_original --lift` is how it is
  taken back. Re-adding a file does not silently lift it, because "I uploaded it again" is not
  "I withdrew my deletion request".
- The export is staff-triggered. Until the customer endpoint exists, "offered an export" means the
  operator runs a command and sends the file.

## Amendment 2026-09-18: a customer can download their own export

Operator decision: instead of asking staff, a signed-in customer downloads their own export
directly. `GET /api/v1/app/export` (`marceline/app/export.py`) calls the exact
`export_document(organisation)` this ADR already specified — no second implementation — and
streams it as a JSON attachment, `organisation_of(request.user)` deciding the scope exactly as
every other customer read does. Development only (`public_surface.ROLE_ONLY_PREFIXES["dev"]`, like
rights proofs and notices): production holds no originals, rights records or notice drafts beyond
its signed customer-cache bundle, so the question this ADR left open ("how a large document
travels through the signed cache") does not have to be answered yet.

Two things this route adds on top of what the staff command already did:

- **Rate limit**: the same per-organisation throttle customer writes use
  (`marceline/app/upload_throttle.py`), a new `Kind.EXPORT` at one per rolling hour — an export
  reads every customer document, every reference frame hash and every notice draft in one call, as
  heavy as an upload.
- **Audit without storage**: one log line per export (organisation, the account that asked, byte
  size, sha256 of the document) and the same `OrganisationEnding.exported_at` stamp the staff
  command writes, now factored into `deletion.export.stamp_exported_at` so both callers share it.
  The document itself is never written to disk or the database by this route.

`AccountOut.can_export_data` (`GET /app/me`) tells both customer apps whether the route would
answer at all, so the "Download my data" link in the shell header is simply absent on the public
host rather than a dead link. The "offered an export" promise is now literally true — the
customer can retrieve it themselves, in addition to the staff path.

## Alternatives rejected

- **Inferring the end of a pilot** from the last invoice, an expired subscription or inactivity.
  This is the one operation in the product that cannot be undone; a subscription that lapses while
  a customer is on holiday is not consent to delete their case files. A human date is the answer
  that fails safe.
- **Deleting everything the moment the pilot ends.** The operator promised an export first; a
  window is what makes that promise real.
- **Moving the previous owner's case file to the new owner** (a handover). It was never theirs to
  hand over, and it is the one part of the switch the new owner's screen would show immediately.
- **Keeping the previous owner's notice drafts alive for a work they lost**, on the grounds that
  the drafts are their own audit trail. Their audit trail is precisely a record of URLs of a work
  that now belongs to somebody else; the window plus the export is how they keep it, outside our
  database.
- **Deleting pages a customer's originals matched.** They are not the customer's data, and a
  pirate site's page does not stop existing because a customer left.
- **A `deleted` flag instead of deletion (soft delete).** Storage limitation is the whole point;
  a soft delete is a row we still hold. The audit record without personal data is the durable part.
- **One combined retention job with `page_retention`.** Different clocks, different scopes,
  different flags; `make status` reads job names.
