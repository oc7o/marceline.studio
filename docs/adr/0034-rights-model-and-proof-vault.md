# 0034 — Rights model, notice gate blockers and the proof vault

Status: accepted (2026-09-17). Operator decision the same day: build the rights model now, send
nothing. Related: issues #33 (notice drafts), #47 (rights ownership and
authorisation), #50 (onboarding, KYC). Builds on ADR 0021 (tenancy), 0025 (case file), 0028
(production cache) and 0031 (ownership column).

## Context

A fingerprint match establishes which work a copy contains. It never establishes **who holds the
right** to that work, whether the claimant is authorised, or whether the use is licensed — and a
takedown notice asserts exactly those things. Being on screen is proof of identity, not proof of
the copyright, so a default of "first performer = rights holder" must not gate notices; there
must be a per-work rights
status with hard blocking states, agency mandates, a licensed-partner list with an explicit "none",
catalogue-level verification, and a restricted store for contracts with strict logging and
retention.

The notice feature itself (#33) is not built here. This change builds the rights
side so that the future "Prepare notice draft" has a gate to ask.

## Decision

### Where it lives: the project app

`marceline/rights/` (models registered through `marceline/models.py`, migrations
`marceline/0006_rights`, `0007_rights_logs_append_only`). Like `OriginalOwnership` (ADR 0031):

- a work is **an organisation's file** — `accounts.Organisation` × a `matching.Reference` sha256 —
  and the gate reads the ownership column. Only the project package may join `accounts`, `matching`
  and the customer ownership rules;
- a new app (`rights → accounts, core`) would have been clean for the tables, but the gate and the
  customer API would still live in the project package, and it needs a new import-linter contract in
  `backend/pyproject.toml`, which is serialised between agents. **No contract changes.**

### Model

| Table | What |
|---|---|
| `RightsClaimant` | one per organisation: claimant type (`studio_producer`, `self_producing_creator`, `agency`, `studio_performer`, `collaboration`), identity status (`unverified`/`verified`/`revoked`), **method** (`commercial_register`, `company_email`, `account_control_token`, `other`) and staff **evidence note**, verifier and date, an account-control token, and the "no licensed partners" affirmation (timestamp + text version) |
| `WorkRights` | per organisation × sha256: `status`, capacity `owner`/`authorised_agent`, underlying owner name, asserted right (enum + free text), optional `schedule` and `mandate`, the self-declaration (version, language, **the exact text**, who, when), `legal_hold`. At most one current (non-`superseded`) record per organisation and work (partial unique constraint) |
| `WorkRightsTransition` | append-only: from → to, actor kind (`staff`/`customer`/`system`), user id + username snapshot, machine code, reason, time |
| `RightsSchedule` | catalogue-level verification: one proof and schedule covering many works, optional end date |
| `AgencyMandate` | principal (+ verified flag and note), agent and authorised person, whole-catalogue flag and description, rights covered, authority to prepare notices, recipient scope, territory, valid from/until, revocation terms, revoked at/reason, reply handling, withdrawal authority, sub-authorisation, data-disclosure acknowledgement, signatory and date, legal hold |
| `LicensedPartner` | first-class rows: kind (`domain`, `platform`, `distributor`, `affiliate`), name, normalised domain, optional path prefix, note |
| `ProofDocument` | metadata of an encrypted document: kind, links to work/schedule/mandate, sanitised filename, sniffed content type, size, plaintext sha256, random storage name, uploader, legal hold |
| `RightsEvent` | append-only organisation-level log: claimant type set, identity verified/revoked, token issued, partner added/removed, none affirmed, proof uploaded, **proof downloaded**, mandate revoked/expired, schedule expired. Codes and ids only |

Logs name people by id and username snapshot, never by foreign key: SET_NULL would be an UPDATE on
an append-only row, PROTECT would make an account undeletable. Both log tables refuse every UPDATE
with a trigger (SQLSTATE 23000), like the GoBD triggers of ADR 0032; DELETE stays possible for
retention only (the admin offers none).

### The status machine

`unverified → self_declared → documents_received → verified`, blocking states `disputed`,
`revoked`, `expired`, `superseded`. `marceline.rights.services.transition` is the only writer of
`status`; every call logs. Allowed moves and actors (`services.ALLOWED`):

- **customer**: `unverified → self_declared`, and re-declaring (`self_declared → self_declared`).
  The declaration must be confirmed and carry the **current text version**; the stored record keeps
  the language and the text itself. Nothing a customer does goes further;
- **staff** (admin actions, each with a confirmation page and a required reason):
  → `documents_received` (from unverified, self-declared, or back from verified), `documents_received
  → verified` (never directly), → `disputed`, → `revoked`, → `expired`, `disputed → unverified |
  documents_received`, `revoked | expired → unverified`, anything → `superseded` (which opens a
  fresh unverified record);
- **system**: → `disputed` (competing claim), → `revoked` (mandate revoked), → `expired` (mandate or
  schedule ended).

Finer intermediate labels (`corroborated`, `document-checked/authorised`,
`notice-enabled`) are folded into `documents_received` and `verified` as the operator specified;
"notice-enabled" is not a status but an empty blocker list.

**Conflict.** Two organisations holding a current, non-closed record for one sha256 is a competing
claim, whoever registered first: every such record moves to `disputed` (`flag_competing_claims`, on
each declaration), and staff can record a claim that arrives outside the product (admin: *Record a
competing claim by another organisation*). Ownership already allows only one owner per file (ADR
0031); the rights record is where a second claimant becomes visible. The customer sees
`competing_claim` as a blocker, never who claims.

**Mandates and schedules.** Revoking a mandate (admin action) moves every linked work to `revoked`.
`manage.py rights_housekeeping` moves works of mandates or schedules whose end date has passed to
`expired`; the gate also reads the dates live, so a missed run never enables anything.

### `notice_gate_blockers(work)`

`marceline/rights/gate.py`. `gate_blockers(GateFacts) -> list[Blocker]` is pure; `notice_gate_blockers
(Work(organisation, sha256))` reads a snapshot (read-only) and evaluates it; `organisation_blockers`
does the same for many works in a fixed number of queries. Codes in a fixed order:
`work_not_owned`, `claimant_type_missing`, `identity_not_verified`, `rights_record_missing`,
`rights_not_verified`, `rights_disputed`, `rights_revoked`, `rights_expired`, `competing_claim`,
`underlying_owner_missing`, `mandate_missing`, `mandate_not_yet_valid`, `mandate_expired`,
`mandate_revoked`, `mandate_without_notice_authority`, `schedule_expired`, `partners_not_affirmed`.

An empty list means only that the **rights side** has no objection. The match side of the
gate (human-confirmed content identity, recent recheck, customer URL confirmation, no known
licence for this use, exception review, recipient and channel adoption, signature, audit record) is
the notice feature's own gate and is not built here.

### Licensed partners

`partners.partner_for_url(organisation, url_or_host)`: hosts are normalised (lowercase, no port,
no `www.`, IDNA); a partner domain covers its subdomains but not look-alikes (`badexample.com`), a
path prefix narrows a shared platform to whole path segments, and the most specific row wins.
Adding a partner clears a "none" affirmation; affirming "none" is refused while partners exist.

### Proof vault

`marceline/rights/vault.py`:

- **Encrypted at rest** with Fernet (`cryptography`, already a dependency): AES-128-CBC with
  HMAC-SHA256. Keys only from `RIGHTS_VAULT_KEYS` (comma-separated; the first encrypts, all decrypt,
  so a key can be rotated by prepending a new one). The plaintext sha256 is stored on the row and
  checked on every read, so a swapped or damaged file is refused.
- **Outside every served directory**: `RIGHTS_VAULT_DIR` must be absolute and outside the
  repository (refused otherwise); files are named by a random token, mode 0600 in a 0700 directory,
  written atomically. On the development VPS the web container uses the named volume
  `rights-vault` (`infra/compose.yaml`). Without both settings uploads answer 503 — nothing falls
  back to plaintext or a default path.
- **Documents, not media**: PDF, PNG, JPEG recognised by leading bytes (name and declared type are
  ignored), at most `RIGHTS_PROOF_MAX_BYTES` (10 MiB). The customer screen says "the rights-relevant
  excerpt … never video material and no ID documents". No ID document is requested by default
  (identity plus connection to the work, not maximal identity data).
- **Staff-only download** through the admin (`admin:marceline_proofdocument_download`): staff
  session and view permission required, the access is logged *before* decrypting, the response is
  an attachment with `no-store`, `nosniff` and `Content-Security-Policy: sandbox`. Customers cannot
  download what they uploaded.

### Customer API and screen

`/api/v1/app/rights` (`marceline/app/rights_status.py`): `GET` overview; `PUT /claimant`; `POST
/claimant/token`; `POST /works/{original_id}/declaration`; `POST /partners`; `DELETE
/partners/{partner_id}`; `POST /partners/none`; `POST /proofs` (multipart). Public ids only (`orig_`, `partner_`, `proof_`; ADR 0033). Codes only (refusals are 409
with a code, 413/415/503 for uploads); every write answers the whole overview; a foreign and an
unknown original or partner are one byte-identical 404; CSRF on every write through session auth.
The declaration texts are server-owned and versioned (`rights/declarations.py`) and the screen shows
them verbatim — what is confirmed is what is stored.

**Development only**: `public_surface.ROLE_ONLY_PREFIXES["dev"]` names `/api/v1/app/rights`, so
production (`MARCELINE_ROLE=prod`) answers the bare 404, like invoices (ADR 0032). Rights records and
proofs never enter the signed bundle; production holds no copy.

`/app/rights` (`frontend/src/app/RightsScreen.tsx`), German and English: claimant type, identity
check state and token, one entry per original with its rights status and what is still missing, a
self-declaration form, partners with "confirm that there are none", uploads. Wording is factual —
"rights status", "self-declaration", "rights verified by our staff" — never "verified
infringement". The add-original screen (#29) keeps its own simple rights tick; this per-work record
is what it can link to later.

### Staff side

Django admin (*Marceline → Rights claimants / Work rights records / Agency mandates / Rights
schedules / Proof documents / Licensed partners / transitions / events*). Status fields are
read-only; every change is an admin action with a confirmation page listing the rows and a required
reason (identity: method and evidence note). Nothing is deletable in the admin; logs are read-only.

### Retention

`marceline/rights/retention.py`, run by `manage.py rights_housekeeping [--dry-run] [--date]`
(expiry first, then retention; idempotent; scheduler job `rights_housekeeping`, daily):

- a **closed** work record (`revoked`, `expired`, `superseded`) is deleted — with its transitions,
  its proofs (vault file, then row) and their access log — from 1 January of the fourth year after
  its last transition, i.e. after the end of the third calendar year following the last relevant
  event (a product policy, not a statutory duty);
- a revoked or ended mandate goes the same way once no remaining record points at it;
- a `legal_hold` on the record, the mandate or any of its proofs keeps it;
- open records, disputed ones included, are never deleted by this job.

## Consequences

- Two migrations in `marceline` (tables; append-only triggers). Apply from the main checkout.
- New settings `RIGHTS_VAULT_KEYS`, `RIGHTS_VAULT_DIR`, `RIGHTS_PROOF_MAX_BYTES` (`.env.example`,
  pinned empty in `OPERATIONAL_DEFAULTS`; tests generate a key and use a temporary directory).
- The vault volume is **not in the Postgres dump**: until it is added to the backup, a lost volume
  loses every proof (the rows would remain and reads would answer `missing`). Losing the key makes
  every proof unreadable — keep a copy of it apart from the backup.
- A customer who declares a file another organisation already claims puts both records into
  `disputed` and sees `competing_claim` — a deliberate, minimal disclosure: the work is contested,
  not by whom.
- Moving an original to another organisation (`assign_originals`) leaves the old organisation's
  rights record behind; the gate reports `work_not_owned` for it, and the new owner's declaration
  disputes both until staff supersede the stale one.

## Alternatives rejected

- **Rights on `OriginalOwnership` / `Reference`.** Ownership is who may *see* an original; rights
  are who may *assert* against copies, with their own history, blocking states and retention.
- **A `rights` app with a new import-linter contract.** Clean tables, but the gate and API stay in
  the project package anyway, and `pyproject.toml` is serialised between agents.
- **Plaintext files behind an unguessable URL / Django `FileField` in `MEDIA_ROOT`.** A served
  directory and a readable disk are exactly what the privacy vault must not be.
- **Automated KYC or ID-document collection.** Not needed to establish identity plus connection to
  the work for studios, agencies and account-controlling creators; it would create a sensitive
  identity store.
- **First registration wins.** No such rule is defensible.

## Open operator decisions

- Generate `RIGHTS_VAULT_KEYS` and decide where its offline copy lives; add the `rights-vault`
  volume (or host directory) to the backup, encrypted as it is.
- Whether a solo creator's account-control token plus self-declaration is enough to reach
  `documents_received` (one corroborator beyond the declaration is required; staff record
  which) — the concrete per-type proof checklist for staff (#47).
- Retention of organisation-level data (claimant, partners, events, unlinked proofs) at account
  closure, and a shorter schedule for superseded duplicate proofs.

## Amendment (2026-09-17, operator): scheduling, the upload tick, withdrawal

- **Scheduled.** `rights_housekeeping` runs as the scheduler's last daily job
  (`SCHEDULER_RIGHTS_HOUSEKEEPING_HOURS`, default 24).
- **The Add original tick is a self-declaration** (#29). `POST /api/v1/app/fingerprints` calls
  `services.declare_at_upload` in the upload's transaction: an unverified record becomes
  `self_declared` (a self-declared one is re-declared), logged with code `declared_at_upload`, and
  the stored declaration is the tick's wording as `upload-<version>` with its language and text.
  `RightsConfirmation` stays the upload's own audit row and points at the record
  (`RightsConfirmation.work_rights`, SET_NULL so retention can still delete a closed record). A
  record staff already work on (documents received, verified, disputed, …) keeps its status: an
  upload never downgrades or reopens it. The tick names no capacity and no asserted right, so the
  gate has a new blocker `declaration_incomplete` for any declared record missing either; the
  customer completes it on the rights screen.
- **Customers may withdraw their own self-declaration**: `self_declared → unverified`
  (`services.withdraw_declaration`, `POST /api/v1/app/rights/works/{original_id}/withdrawal`) with
  a reason code (`rights_transferred`, `authority_ended`, `declared_in_error`, `other`), an optional
  note and the confirmed, versioned withdrawal text (`declarations.WITHDRAWALS`), all logged. The
  record's declaration fields are cleared; the log keeps what was declared. Once staff received
  documents or verified the record, only staff change it (409 `declaration_not_withdrawable`).
- The customer router moved to `marceline/app/rights_status.py`: #29 owns `marceline/app/rights.py`
  (the tick's wording). Migrations renumbered to `marceline/0006_rights` and
  `0007_rights_logs_append_only`, after #29's `0005_customer_uploads`.

## Amendment (2026-09-18, issue #46): the document check is formal, never substantive

Staff do not decide whether a contract or mandate legally supports ownership or authority, and
the product never tells a customer what their documents mean. Before this amendment the
admin's *Verify rights* action asked staff to agree that "the documents show this organisation
holds, or is authorised for, the asserted right in each work", and the blocker
`mandate_without_notice_authority` read "The agency mandate does not cover notices." /
„Die Agenturvollmacht umfasst keine Meldungen."

**What changes now (wording only, no new state, no gate logic):**

- **What staff are asked.** The admin action is *Record the formal document check* and names a
  formal-completeness checklist — parties named, work or catalogue identified,
  date and signature present, period still running — and says in as many words that whether the
  wording legally supports the asserted right is not decided there: if that is the open question,
  the record stays in `documents_received`. The reason field asks what was seen, not what was
  concluded. The schedule action says the same for a catalogue. Its transition code is
  `formal_check`, so the append-only log distinguishes rows recorded under the old semantics
  (`verified`) from rows recorded under these.
- **What the mandate fields mean.** `authority_to_prepare_notices` is a presence check — the
  document names notices in its own words — and is left unticked where that would take
  interpretation. `principal_verified` is identity only (it never fed the gate). `rights_covered`
  is a transcription. Help text only: migration `marceline/0014_mandate_formal_check_help_text`.
- **What the customer is told.** The rights screen says staff check the documents *for
  completeness* and that we do not assess what they mean legally. The blocker for a mandate with no
  notice clause recorded now states our own record ("Our record of this mandate has no entry for
  notices. We do not assess what your documents mean legally — please write to us."), in both
  languages and in both places the code lists blockers. `frontend/src/i18n/wording.test.ts` gained
  a rule per language that fails the build on any customer string saying what a mandate, contract,
  licence or document covers, authorises or permits — in either direction.
- **The gate is unchanged.** It still derives only formal facts: ownership, presence of a record,
  status, count of competing claims, mandate dates and revocation, schedule dates, the partner
  affirmation. `notice_gate_blockers` reads and never writes, and no code was added or removed.

**What this deliberately does not do.** A layered model (`self_declared` / `document_present` /
`formal_fields_checked` / `authority_confirmed_by_customer`) is not implemented here. The missing
layer is the customer's own express confirmation that their documents give the authority they
claim. That is a product decision with a new customer write, new states, new blocker codes and a
migration. Until then the status keeps the name `verified` although it covers a formal-only
check. Renaming the status and its blocker codes belongs with that same change, not before it, so
the codes move once.

## Amendment (2026-09-18, issue #46): the customer declares their own authority

The amendment above stopped staff deciding what a document supports. That left a hole: with the
check formal-only, **nobody** was making the affirmative statement that the documents give the
claimed right — and that statement must exist and must be the customer's. This adds it.

### The status machine grows one step, and two names become honest

`unverified → self_declared → documents_received → documents_complete → authority_declared`,
blocking states unchanged.

- **`verified` is now `documents_complete`.** It means what staff actually record: parties, work,
  dates and a signature present, the period still running. Migration `marceline/0015` renames the
  value on every row; `WorkRightsTransition` is **not** rewritten — it refuses UPDATE by trigger,
  and rightly so, because the log says what was recorded at the time, under the semantics of that
  time.
- **`authority_declared` is new and is the customer's.** `ALLOWED` admits `ActorKind.CUSTOMER`
  only, in both directions — not staff "on the customer's behalf", because a staff member writing
  it would be us making the assertion again through a different door. There is no admin action, no
  management command and no system path to it; a customer who cannot use the screen writes to the
  operator, and nothing is recorded until they can. A test asserts that both staff and system
  transitions to it are refused.
- **Leaving it clears the declaration** (`services.transition`, `AUTHORITY_FIELDS`): a reopened or
  disputed record never shows a statement the customer has not made about the documents now on
  file. The log keeps what was declared and when.

### The words, and who owns them

`rights/declarations.py` gains `AUTHORITY_DECLARATIONS` and `AUTHORITY_WITHDRAWALS`, versioned and
never edited in place like the others; the exact text shown is stored on the record with its
language. The text is the customer in the first person — *"I have read the documents … and I state
that they give it the right or the authority I declared here … Nobody at Marceline has assessed for
me what these documents mean in law"*. That asymmetry is deliberate and is now written at the top
of the file: **a declaration may say what a customer's own mandate covers; no string of ours ever
may** (`frontend/src/i18n/wording.test.ts`). The proposition belongs to whoever makes it.

### Blocker codes: renamed once, together

`rights_not_verified` → `documents_not_complete`, plus the new `authority_not_declared`, and
`mandate_without_notice_authority` → `mandate_notice_clause_not_recorded` (the staff tick behind it
has been a presence check since the previous amendment, and the code now says so). Each is a fact
about **our record**, which is the rule the gate's docstring states. The gate needs no new fact:
both halves are read from `WorkRights.status`, and `authority_declared` is simply the one open
status with no blocker.

### Customer API, and the production write path

`POST /api/v1/app/rights/works/{original_id}/authority` and `…/authority/withdrawal`, answering the
whole overview like every other rights write, with `authority_declaration` and
`authority_withdrawal_declaration` in it and `authority_declared_at` / `authority_declaration_version`
per work. On production both go through the outbox as `rights_declare_authority` /
`rights_withdraw_authority` (`delivery/0005`), applied by development through the same
`rights.services` call — the ADR 0028 write path, unchanged. Both frontends grew the two forms
(`/app/rights`), the Next app two BFF routes.

### What a customer has to do that they did not before

One extra, deliberate step per work, after we tell them their documents are complete: read the
declaration and tick it. Until they do, the rights gate reports `authority_not_declared` and no
notice draft can be prepared — by design, and it is the point of the change rather than a side
effect. They can take the statement back at any time; the documents and their formal check stay.

### Still open

`mandate_notice_clause_not_recorded` remains the one place where **our** reading of a document —
even a purely formal "does it name notices in its own words" — still blocks a customer who has now
made their own authority statement. Whether that residual check should gate at all, or only be
recorded, is undecided.
