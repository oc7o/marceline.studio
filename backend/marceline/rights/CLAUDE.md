# Rights model and proof vault

Moved verbatim out of `backend/CLAUDE.md` on 2026-09-18 so that file stays small. The root `CLAUDE.md` hard
rules and `backend/CLAUDE.md` apply here too.

## Rights model and proof vault (ADR 0034)
- **A match says which work; it never says who holds the right.** `marceline/rights/` records that
  separately and gates the future "Prepare notice draft" (#33). Nothing here sends anything.
- Tables (project app, registered via `marceline/models.py`): `RightsClaimant` (claimant type,
  manual identity check: method + evidence note, account-control token, "no partners" affirmation),
  `WorkRights` (organisation × **sha256**, one current record), `WorkRightsTransition` and
  `RightsEvent` (**append-only: UPDATE refused by triggers**, migration `marceline/0007`),
  `RightsSchedule` (one proof, many works), `AgencyMandate`, `LicensedPartner`, `ProofDocument`.
- **`rights/services.py` is the only writer.** `transition(work, to, actor, reason=, code=)` checks
  `ALLOWED` and logs; customers only reach `self_declared` (current declaration version required,
  text snapshot stored) and may withdraw it again (`withdraw_declaration`: `self_declared →
  unverified`, reason code + withdrawal text version logged; not once staff have documents);
  the Add original tick (#29) self-declares through `declare_at_upload` (code
  `declared_at_upload`, never downgrades a record staff work on; `RightsConfirmation.work_rights`
  links the upload's audit row to the record; no capacity/right → blocker
  `declaration_incomplete`); `verified` only from `documents_received`; staff need a reason; system
  moves are `competing_claim` (two organisations with open records for one sha256 → both
  `disputed`), `mandate_revoked`, `mandate_expired`/`schedule_expired`. Never set `status` directly.
- **Two halves, two hands** (ADR 0034 amendment 2026-09-18): `documents_complete` (ours, formal —
  it was called `verified`) and `authority_declared` (**the customer's own statement** that those
  documents give them the right or authority they declared). `services.declare_authority` /
  `withdraw_authority` accept `ActorKind.CUSTOMER` only — there is no staff, admin or system route
  to `authority_declared`, deliberately — and leaving that status clears the stored declaration.
  Blockers: `documents_not_complete`, `authority_not_declared`,
  `mandate_notice_clause_not_recorded` (renamed together; each is a fact about **our record**).
- **The document check is formal, never substantive** (ADR 0034 amendment 2026-09-18): staff record that a document is *complete* — parties, work, date, signature, period still
  running — never that it legally supports the claimed right. `authority_to_prepare_notices` is a
  presence check ("the mandate names notices in its own words"), unticked where the answer would
  take interpretation. No customer string may say what a mandate, contract or licence covers or
  authorises, in either direction (`frontend/src/i18n/wording.test.ts` enforces it in both
  languages). Reintroducing a substantive check needs the operator and counsel.
- **Gate:** `gate.gate_blockers(GateFacts)` is pure; `notice_gate_blockers(Work(organisation,
  sha256))` / `organisation_blockers` read a snapshot, never write. Blocker codes are an enum in a
  fixed order; add one there, in `app/rights.BlockerCode` (asserted equal at import) and in both
  languages. The match-side gate (human-confirmed, recheck, customer adoption) is not in here.
- `partners.partner_for_url(organisation, url)`: normalised hosts, subdomains covered, look-alikes
  not, optional whole-segment path prefix, most specific row wins.
- **Vault** (`rights/vault.py`): Fernet with `RIGHTS_VAULT_KEYS` (first encrypts, all decrypt),
  `RIGHTS_VAULT_DIR` absolute and outside the repository — checked by the shared
  `core.checkout_paths.inside_checkout` (issue #59's cloud-fingerprinting trial, 2026-09-18, found
  that a bare ancestor test against `BASE_DIR`'s parent calls *every* absolute path "inside the
  repository" in a container, where that parent is the filesystem root; the upload spool
  (`marceline/uploads/spool.py`) had the identical bug and shares the fix) — random storage names,
  0600/0700, digest checked on read; PDF/PNG/JPEG by leading bytes, ≤ `RIGHTS_PROOF_MAX_BYTES`.
  Unconfigured = 503. **Only `services.read_proof` decrypts, and it logs the access first**; the
  only download is the staff admin view. Proofs are documents, never media. Tests:
  `rights_helpers.configure_vault` with a generated key and `tmp_path`; `OPERATIONAL_DEFAULTS`
  pins the settings empty.
- Customer API `marceline/app/rights_status.py` (`/api/v1/app/rights`, codes only, every write returns the
  overview, 404 byte-identical, declaration texts from `rights/declarations.py` — a new wording is a
  new version, never an edit). **Development only** via `ROLE_ONLY_PREFIXES["dev"]`; rights data
  and proofs never go into the signed bundle.
- Admin: status changes only as actions with a confirmation page and reason; nothing deletable;
  logs read-only. Retention: `rights_housekeeping` (expiry, then closed records after the end of the
  third calendar year following their last event unless `legal_hold`), scheduler job daily.
- **`legal_hold` is refused by the database since ADR 0045** (`marceline/0019`): a `BEFORE DELETE`
  trigger on `WorkRights`, `ProofDocument`, `AgencyMandate` and `RightsSchedule` raises on a held
  row, for the admin, raw SQL and any future code path alike. Lift the hold deliberately first.
- **Account closure is no longer a follow-up** (ADR 0045): `customer_data_retention` deletes an
  ended organisation's whole rights block — claimant, partners, mandates, schedules, works,
  proofs (vault file first, as `rights_housekeeping` does) and organisation-level events — but only
  when **nothing** of that organisation is on legal hold. One held row keeps the whole block, and
  keeps the organisation row alive through `PROTECT`; the pass reports it as `legal_hold`.
- The router module is `marceline/app/rights_status.py`; `marceline/app/rights.py` is #29's
  upload-tick wording (`rights_confirmation.json`).

## Account-control verification (ADR 0055, `ACCOUNT_CONTROL_VERIFICATION_ENABLED`, off)
- **Identity is established by proof of *control* of the creator's platform account,
  never by a document.** A one-time code goes into that account's bio (or arrives from it by DM), a
  staff member opens the **public** profile **by hand** and records what they saw. **No identity
  document and no date of birth is stored anywhere** — there is no field for one and
  the note's help text says so (operator decision 2026-09-19). The `video_call` fallback records
  only *that* a check happened: date, staff user, method.
- **One notion of "verified", still `RightsClaimant.identity_status`.** `account_control_services.
  verify` calls `services.verify_identity`, so the gate's `identity_not_verified` clears through the
  field it always read. **Never add a second verified flag.** ADR 0034's admin path stays and reads
  `staff_legacy`, which a database check constraint refuses as a *challenge's* method.
- `rights/account_control.py` is pure (the fixed platform list with each platform's own hosts, the
  handle and URL shapes, the code alphabet, the 72 h lifetime); `account_control_services.py` is the
  only writer; `AccountControlEvent` is **append-only by trigger** (`marceline/0027`).
- **Nothing in this flow fetches anything.** A claimed `profile_url` is inert text for a human to
  open — no crawler, no HTTP client, no preview — and `test_account_control.py` fails if
  `account_control.py`, `account_control_services.py` or `models.py` imports something that can open
  a socket. The host allowlist is a typo guard, not a boundary for a request that never happens.
- **A platform that checks nobody gets no code** (operator review 2026-09-19): `own_website` and
  `other_platform` have no identity check behind them, so a placed code proves nothing about who the
  creator is. No code is issued, `mark_placed` refuses (`video_call_required`), and only
  `video_call` may settle them — in the list (`Platform.checks_identity`), in `verify`, and in a
  **database constraint**. `checks_identity` and "has hosts" are asserted equal by a test: naming a
  host for a platform is saying we believe it checks its creators.
- **The code is stored in the clear on purpose**: staff compare it by eye. It is a nonce, not a
  credential — 72 h, one claim, replaced by a reissue (which is what invalidates the old one),
  dropped when the claim settles, never in the event log.
- A read never settles an expired code: `display_state` is pure and `expire_if_due` / `expire_due`
  write. `expire_if_due` runs **outside** the caller's transaction, or the refusal would roll back
  the row that records why.
- Customer surface `marceline/app/verification.py` (`/api/v1/app/verification`): 404 everywhere while
  the switch is off; on production the status comes from the signed bundle (`verification` document)
  and the two writes go through the outbox (ADR 0046's pattern). The switch must be set on **both**
  machines.
