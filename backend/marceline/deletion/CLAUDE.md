# Customer data deletion

The root `CLAUDE.md` hard rules, `backend/CLAUDE.md` and `backend/marceline/CLAUDE.md` apply here
too. ADR 0045, issue #85.

## Customer data deletion (ADR 0045)
- **Two scopes, one clock each, both set by a human.** `OrganisationEnding.ended_at` (a pilot
  ended) and `OriginalRemoval.removed_at` (one work of one organisation, keyed by **organisation ×
  sha256** so it outlives the ownership row). Deletion happens at clock +
  `CUSTOMER_DATA_RETENTION_DAYS` (30). **Nothing infers a date** — not a lapsed subscription, not a
  last login, not an empty account: this is the one operation in the product that cannot be undone.
  Never add such an inference; if a customer-triggered "close my account" arrives, it sets the same
  `ended_at` and the window still applies.
- **Marking is not behind the flag; deleting is.** `services.end_pilot` / `remove_original` (and
  their `lift_*`) write a date and delete nothing. `retention.apply_retention` deletes, from the
  scheduler's `customer_data_retention` job, last of all, only while
  `CUSTOMER_DATA_DELETION_ENABLED`. Both settings are passed through `infra/compose.yaml`'s
  `scheduler` service and pinned by `marceline/tests/test_compose_env.py` — a setting compose names
  to nobody is empty inside the container whatever `.env` says.
- **What is never deleted, in either scope**: pages, page media, page frame hashes, clusters,
  common frames and breadcrumbs (ours, not the customer's — `page_retention` owns them); a file
  another organisation owns a reference of (`_shared_files`, and the reference set is filtered by
  ownership *at deletion time*); billing (ADR 0032 — invoices, subscriptions and the billing
  profile all `PROTECT` the organisation, so the organisation row survives its own pass and the
  report says so); every rights row of an organisation **any** of whose rights rows is on
  `legal_hold` (ADR 0034: a held work's mandate, schedule, proofs and log are the record, and
  picking it apart is how a hold gets lost); safety incidents; staff accounts.
- **A kept row is reported, never silently skipped.** The codes are constants in `retention.py`
  (`KEPT_BILLING`, `KEPT_LEGAL_HOLD`, `KEPT_SHARED_FILE`, `KEPT_STAFF_ACCOUNT`,
  `KEPT_ACCOUNT_PROTECTED`, `KEPT_ORGANISATION`) and land in `DataDeletionRecord.kept`. A pass that
  kept something back leaves the mark **pending** (`complete=False`), so the job finishes the job
  by itself once the hold or the invoice is gone.
- **An owner switch is the common case of the original scope** (`handover.py`, called by
  `ownership.reassign`). Two effects at two times, and the split is the decision: the **case file
  is deleted at once** (`matching.LeakCase`, `CaseFinding` — it hangs off the `Reference`, which
  moves, so the new owner would read the previous owner's stamps the moment the move commits;
  `sync_cases` rebuilds one from the match rows), and **everything else is only marked**. Never
  make the mark delete immediately "for symmetry": the window is what makes the export real. The
  dry run of `assign_originals` prints both, computed **before** the move — after it the rows are
  gone and the report would print zeroes.
- **Database guards, not only Python** (`marceline/0019`): `DataDeletionRecord` refuses UPDATE and
  DELETE (a record of a deletion that could be deleted answers nothing), and a `legal_hold` row of
  `WorkRights` / `ProofDocument` / `AgencyMandate` / `RightsSchedule` refuses DELETE outright. Lift
  the hold deliberately first. The invoice trigger (`billing/0002`, `0004`) is untouched and is
  never worked around.
- **The audit record carries no personal data**: the organisation's primary key and public id as
  they were, a digest, dates, counts and codes. No name, no username, no URL, no title. Keep it
  that way — it is the one row that outlives the deletion.
- **The export** (`export.py`) renders the customer's own documents through
  `marceline.export.render`, the same function ADR 0028's bundle uses, so what a customer gets is
  exactly what the product showed them — never a second serializer that could show more. The
  fingerprint section is stored hashes in the client document's frame shape and says in its own
  `note` that it is not a re-importable client document (a `Reference` stores no file size or image
  count). Two callers, one implementation: `manage.py export_customer_data` (staff) and `GET
  /api/v1/app/export` (`marceline/app/export.py`, ADR 0045 amendment 2026-09-18, dev-only like
  rights and notices) both call `export_document` and both stamp `OrganisationEnding.exported_at`
  through `stamp_exported_at` — never duplicate either.
- **The export carries the claimed platform accounts** (ADR 0055): `account_verification`, one entry
  per claim with its append-only event log, and **no code field** — a settled claim has none and a
  live one is on the customer's own screen. The rights block's deletion takes the challenges with
  it, named in `_delete_rights` rather than left to the organisation row's CASCADE, because one
  legal hold keeps that row and the handles must not be the last thing standing.

## The creator's own data requests (ADR 0056, issue #133)
- **A request is a request — it exports nothing and deletes nothing.** `CustomerDataRequest` records
  that a creator asked for a copy of their data (Art. 15, Art. 20) or for its deletion (Art. 17),
  when, and where staff stand with it. ADR 0045's rule above is unchanged and is the reason: the
  clock is still a date a human sets with `end_pilot`, and the export is still
  `export_customer_data` plus a delivery the operator makes. A button that started either would put
  the one irreversible operation in the product behind a session cookie on the public host.
  `requests.py` is the only writer; `open_request`, `withdraw`, `start`, `settle`, `waiting`.
- **Why it exists at all:** on the public host `can_export_data` is false (the export route is
  `ROLE_ONLY_PREFIXES["dev"]` — production holds no originals) and there was no deletion route
  anywhere, so an invited creator could exercise neither right from inside the product. It is the
  one gap in that journey with a statutory deadline: Art. 12(3) GDPR, one month, `DEADLINE`.
- **One open request per organisation per kind**, enforced by the partial unique index
  `data_request_one_open_per_kind` — a second press is not a second request (409 `already_open`),
  and a settled one is never rewritten. A deletion needs the typed `DELETION_PHRASE`, which is
  **deliberately untranslated** because the bundle document is language-independent, and it is
  checked again in `customer_writes.py`: a write that crosses machines is judged where it acts.
- **`CONTACT_EMAIL` and `DELETION_PHRASE` are pinned constants the API hands out**, so no UI string
  spells the address and no screen invents the word. The address is the one both legal documents
  name (privacy notice C1, pilot agreement § 16 Abs. 7).
- It reaches production like rights and notices: the `data-requests` bundle document, two outbox
  kinds, no `ROLE_ONLY_PREFIXES` entry — and `CUSTOMER_DATA_REQUESTS_ENABLED` has to be set on
  `web`, the public host **and** development's `export` service, which is where
  `pull_customer_writes` applies one (`test_compose_env.py` pins all three).
- The requests travel in the export (`export.data_requests_of`) and are deleted with the
  organisation, named in `retention.delete_organisation` rather than left to the CASCADE: a legal
  hold can keep the organisation row, and a list of "delete my data" requests must not outlive the
  deletion it asked for.
- **The daily operator report counts them** (`crawler/safety_digest.py`, "== Customers =="), so a
  request cannot sit unseen. `crawler` may not import `marceline`, so the digest holds a hook that
  `marceline/apps.py` fills; the counter ignores the switch on purpose — a request recorded while it
  was on is still owed an answer after it is turned off.
