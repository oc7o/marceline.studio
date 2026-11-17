# The project package (`backend/marceline/`): the full reference

`backend/marceline/CLAUDE.md` keeps the invariants an agent must not break; this file is the detail
it used to carry — field lists, priority arithmetic, endpoint inventories, settings names and the
narrative each rule grew out of. Moved here verbatim on 2026-09-18 (issue #79); nothing was removed.
The decisions live in the ADRs (0019, 0021, 0023, 0028, 0029, 0031, 0033).

---

# Project package: targets, prospects, customer API, login, production surface

Moved verbatim out of `backend/CLAUDE.md` on 2026-09-18 so that file stays small. The root `CLAUDE.md` hard
rules and `backend/CLAUDE.md` apply here too.

## Targeted indexing (issue #71, ADR 0029)
- **`marceline/targets.py` is the single answer to "whom do we hunt".** `targets()` = rights
  holders (primary creator of an original some organisation owns, via
  `marceline.app.ownership.owned_references`, the organisation column) at `SEED_PRIORITY`; co-performers of owned
  originals below them at `co_performer_priority(originals, partners)` (a pure function with named
  constants, 12..28); `catalog.WatchlistEntry` creators **only with `crawl` ticked**, at
  `WATCHLIST_SEED_PRIORITY` (11). The watchlist otherwise notes interest: no seeds, no active
  pages, breadcrumbs only. Crawled entries with `purpose=prospect` are `TargetKind.PROSPECT` at
  `PROSPECT_SEED_PRIORITY` (11, listed last) and `crawl_seed` queues only
  `prospect_seed_plan(target, now=)`: at most `PROSPECT_DAILY_SEED_CAP` of their seeds pending,
  running or fetched in the last 24 h, across sites. `target_seed_urls`/`keep_rules` use the
  uncapped seed list. `crawl_seed` (project package) seeds only targets unless `--creator`
  names someone; `crawler.services.enqueue_seeds` keeps a pending seed at its current priority.
- **Deactivate, never delete — but not kept forever either (ADR 0029 amendment 2026-09-17).**
  `index.Page.active` / `inactive_reason` / `active_changed_at`; new pages start active.
  `keep_rules` (linked, matched, clustered, seeded, safety) decides; `deactivate_untargeted`
  applies it and reactivates, `sync_targets` schedules it once `TARGETED_INDEXING_ENABLED`. A
  non-clear page is never deactivated. `apply_activity` also scrubs a newly deactivated page
  (`index.retention.scrub_deactivated_pages`): drops `title`, `tags` and the performer credits in
  `raw_extraction`; keeps the URL, every timestamp, the site, every hash and — unlike those —
  `Page.creators`, since the `linked` rule needs that link to reactivate the page later (by a
  fresh seed, a PDQ/SHA256 match or a cluster link work too, without it). `Page.deactivated_at` is
  the retention clock:
  set on deactivation, cleared on reactivation; `page_retention` (CLI, scheduler job guarded by
  `PAGE_RETENTION_ENABLED`) deletes the row outright `index.retention.PAGE_RETENTION_MONTHS` (24)
  after that, `Media`/`FrameHash` cascading with it. Two guards: a page still attached to an
  `open`/`reported` `SafetyIncident` is excluded up front (`OPEN_SAFETY_STATUSES`, a plain query —
  `keep_rules.safety` already keeps a non-`clear` page active forever, this is for the gap where
  staff reversed the page's own block but nobody closed its case yet); a page a customer has
  drafted a real notice about is protected from deletion at the database level
  (`NoticeFinding.page`, `PROTECT`), discovered only on the attempt. Neither `SafetyIncidentPage`
  (`page`, `SET_NULL`, keeps the URL on purpose) nor `SafetyIncidentHash` (no FK to `Page` at all)
  needs a guard of its own — a page's row can already go without corrupting either.
- **Every page-side query in matching filters `media__page__active=True` / `active=True`**: frame
  loaders, mirror groups, SHA256/link evidence, common frames, leak suggestions, `claim_media`,
  capture triage. Loaders that take explicit page ids from human labels do not. A new loader must
  do the same, and incremental scans must keep treating `active_changed_at >= since` as new.
- Breadcrumbs: `index.SiteCreatorBreadcrumb` via `index/breadcrumbs.py` — counts and dates for
  every creator, no URLs; the crawler recounts on each creator link, `--apply` rebuilds. Since the
  amendment above, every counting query filters `page__active=True`: `Page.creators` itself is
  never touched by deactivation, but a breadcrumb only ever reflects that creator's currently
  active pages, so a human browsing it sees no trace of who a now-deactivated page named.

## Prospect research
- `marceline/prospects.py` + CLI `prospect_report`: read-only, staff-only. **Attribution by names
  and PDQ clusters only — never faces or any biometric processing.** `attribution_strength` is
  pure (`strong`: name/alias as whole words in performer credits or tags, or listed on the
  creator's LISTING seed and pagination whose URL still names them; `title`: title only);
  `tally`, `ambiguous_names`, `claim_blockers`, `build_claim`, `headline`, `sample_page_ids`
  (sha256(seed, id) ranking), `render_text` and `render_briefing` are pure.
- Safety-flagged pages are excluded from every count and the sample. Output carries page ids,
  domains and counts — never media, titles or URLs. The claim uses `strong` only; never build
  a published number from the loose total.
- **The claim gate refuses by default** (2026-09-18).
  `claim_blockers` returns codes — `research_basis_not_stated` (`--basis` has no default),
  `no_strong_attribution`, `name_not_distinctive`, `one_site_only`, `sample_unchecked`,
  `precision_unknown`, `precision_below_floor` — and `headline` prints them instead of a count.
  **There is no branch that formats the number without its "as of" date and measured precision**
  (a missing precision is itself a blocker); a test walks every combination. Say "approximately",
  never "at least": the pipeline both misses pages and attributes wrongly. Thresholds are named
  constants (`CLAIM_MIN_SITES`, `CLAIM_MIN_PRECISION`, `MIN_DISTINCTIVE_CHARS`) and are product
  policy, not statutory limits.
- `ResearchBasis` is an operator assertion per run, **never stored**. `render_briefing` is the
  operator-facing summary page: counts only — no page ids, no domains, no hosters.
  Nothing here sends anything; there is no contact automation and there must not be one.
- Sample verdicts: `index.ProspectSampleCheck` (creator, page, verdict `correct|wrong|unsure`),
  admin only. Studio names live in `Creator.aliases`.

## Customer API and tenancy (ADR 0021)
- `accounts.Organisation` owns originals; `accounts.Membership` links a `auth.User` to it with a
  role. Professional (one user) and Enterprise (several) are the same model — **never branch on
  `segment`**, it is billing and onboarding only. A user belongs to at most one organisation
  (database constraint), so a request's scope has exactly one answer.
- **Ownership is a column (issue #76, ADR 0031): `marceline.OriginalOwnership`**, one row per
  assigned original (reference one-to-one as primary key, `organisation` FK with `PROTECT`,
  `assigned_via` / `assigned_by` / `assigned_at`). It lives in the project app because `matching`
  may not import `accounts` — never add a string FK from `Reference` to `accounts` instead, it
  dodges the linter and breaks the contract. No row = unassigned = invisible to every customer.
- **`marceline/app/ownership.py` is the single definition and the only writer.** Reads:
  `owned_references`, `owned_originals` / `owned_original`, `assigned_references`, `owner_of`.
  Writes: `register` (the owner chosen at creation, never overwrites), `owner_for_new` (explicit
  choice, else inherited from an earlier reference of the same sha256), `reassign` (deliberate,
  logged, via `assign_originals` or the admin with confirmation) and `with_twins`. **Membership
  never moves originals, and `Reference.uploaded_by` decides nothing** — it is audit data. Nothing
  else filters originals by user, membership or the ownership table.
- **A move costs the previous owner, deliberately (ADR 0045, issue #85).** `reassign` calls
  `marceline.deletion.handover`: the moved file's case file (`LeakCase`, `CaseFinding`) is deleted
  **in the same transaction** — it hangs off the `Reference`, so the new owner would otherwise read
  the previous owner's `opened_at`, reopen history and per-finding stamps the moment the move
  commits; `sync_cases` rebuilds one from the match rows. Everything else the previous owner holds
  about that file (notice drafts, rights record, public id, refused claims) is **marked**
  (`OriginalRemoval`, reason `owner_changed` / `unassigned`) and deleted
  `CUSTOMER_DATA_RETENTION_DAYS` later, so an export can still be offered. `assign_originals`
  prints both before it acts — the plan is computed **before** the move, never after (afterwards
  the rows are gone and it would print zeroes).
- Registration chooses the owner explicitly: `POST /api/v1/originals` requires `organisation_id`
  (nullable = unassigned; `GET /api/v1/originals/organisations` lists the choices); `add_reference`
  / `import_fingerprints` (project package, because they record the owner) take `--organisation`
  and otherwise inherit like the cast.
- **One customer original per file.** `owned_originals` groups an organisation's references by
  sha256; the newest reference on a current pipeline (`SUPERSEDED_PIPELINES` excluded while a
  current one exists) is the representative whose title customers see.
  `originals.confirmed_findings_of(originals)` merges findings per page across the members
  (confirmed on any member counts, earliest dates win); the original's id is its file's public id
  (below), never a member's primary key. The `LeakCase` lives on the
  representative: `cases._case_of` moves it when a newer reference appears and merges cases two
  members grew, keeping the earliest stamps. `confirmed_findings_for(reference_ids)` stays for
  internal per-reference callers (hoster lookup priority).
- The customer API is a **separate router** (`/api/v1/app/…`, `marceline/app/`), never a widened
  review endpoint. Every customer view starts at `owned_originals` / `owned_original`.
- **Customers see public ids, never primary keys (issue #77, ADR 0033).** `core/public_ids.py`:
  `<prefix>_<32 hex>` (UUIDv4), `public_id_field(prefix)` = unique, non-editable, Python default
  **and** `db_default` (a new column backfills existing rows itself; stale workers still insert),
  `is_valid(prefix, value)`, `missing(prefix)` (well-formed, never generated). Kinds: `org_`
  (`Organisation.public_id`), `inv_` (`billing.Invoice.public_id`), `page_` (`Page.public_id`, a finding's `page_id`), `orig_`
  (`marceline.OriginalPublicId`, keyed by **organisation + sha256** so it survives a new
  representative or a retired twin; written by `register`/`reassign`, filled on read by
  `public_ids_for`; a moved file gets a fresh id in its new organisation). Look up originals only
  with `owned_original(organisation, public_id)`: malformed, integer, unknown and foreign ids are
  all the same 404 (`test_app_public_ids.py`). A new customer-visible object gets its own prefix
  via `public_id_field`; never put an integer id into a customer schema or path. Staff/review APIs
  keep integers. Tests: `app_helpers.public_id_of(reference)`, `UNKNOWN_ORIGINAL`, `grouped(...)`.
- **Cross-organisation access returns 404, never 403** — a 403 confirms the row exists. The body is
  identical to the one for an unknown id (`marceline/tests/test_app_tenancy.py` asserts the
  equality). Staff are not customers: `is_staff` opens the review API, not another tenant's view.
- Customer responses carry **no media of any kind** (issue #53) and never mention a safety-flagged
  page — not even as a count (`marceline/app/safety.py`, ADR 0018).
- **No confidence insight for customers** (operator, 2026-09-17): a finding's evidence is only the
  code `evidence: fingerprint_match | no_fingerprint_match` (`originals.evidence_of`). Never add a
  band, confidence, frame count or distance to a customer schema. A confirmed match stays a
  finding whatever band matching re-assigns it (`counts_as_match`).
- **Customers add originals** (issue #29, ADR 0019 + 0028 amendments): `POST /api/v1/app/fingerprints`
  (`marceline/app/fingerprints.py`) takes the browser's hashes-only document and imports it through
  `from_json` → `import_fingerprint` → `ownership.register(via=CUSTOMER)` for the caller's
  organisation — never duplicate that path. Rules: only `BROWSER_PIPELINE` videos with 8
  orientations at `SUPPORTED_FPS`, ≤ `MAX_POSITIONS`, bodies up to `MAX_BODY_BYTES` on this route
  only (`allow_large_body`); the current rights declaration (`marceline/app/rights.py` +
  `rights_confirmation.json`, never edit a version's words in place — add a version) stored as
  `marceline.RightsConfirmation`; a file the organisation already owns → 200, nothing imported;
  another organisation's or an unassigned file, or ≥ 50 % overlapping hashes → one generic 409
  `claim_needs_review` that names nobody; per-organisation rate limit
  (`marceline/app/upload_throttle.py`, `UploadAttempt`). The registration itself is
  `register_original(user, organisation, payload)` — the endpoint on dev and every outbox item call
  it. **On `MARCELINE_ROLE=prod`** writes go through the outbox (below): 202 with a `sub_…`
  submission when `OUTBOX_ENABLED`, else 503 and `me.can_add_originals` false. Codes only in
  responses. The refused claim itself is persisted staff-only as `marceline.RefusedClaim` (reason
  `owned_by_other` / `unowned_existing` / `hash_overlap`, overlap share, the original(s) touched),
  written by `register_original` on either machine (dev directly, or once development applies the
  outbox item), queued at `GET /api/v1/review/claims` (`marceline/review/claims.py`, ADR 0019:
  never surfaced to any customer endpoint) with resolve/dismiss actions and a staff note.
- **`catalog.Creator.needs_review`**: names a customer typed that matched no creator by exact name
  (aliases never match). Excluded from `targets()`, `crawler.services.cached_creators` (page
  linking) and discovery until staff use the admin action "Mark reviewed" or the equivalent
  `POST /api/v1/review/creators/{id}/mark-reviewed`. The staff queue
  (`GET /api/v1/review/creators`, `marceline/review/creators.py`) also offers merging a duplicate
  into an existing creator or recording it as an alias — both move every reference cast link and
  page link to the target, union aliases, log the move (`marceline.CreatorMergeLog`) and delete the
  source; neither is automatic. A new creator-reading path that hunts, links or searches must
  exclude `needs_review` creators too.
- Test data: `manage.py seed_test_customer --reference ID …` (idempotent; assigns only originals
  named by id that nobody owns, and never moves a user between organisations). In tests,
  `marceline/tests/app_helpers.make_reference(owner, organisation=)` creates the ownership row
  (defaulting to the owner's organisation) and `own(reference, organisation)` assigns one.

## Production settings and the public surface (ADR 0028)
- `DJANGO_ENV=prod` refuses to start unsafe: `core/deploy.py` (pure, `production_problems`) lists
  every problem — secret key or database password that is short or a placeholder, missing or
  wildcard hosts, non-https CSRF origins, secure cookies / HTTPS redirect / HSTS turned off,
  `LOGIN_TRUSTED_PROXIES` < 1, no explicit `DJANGO_PUBLIC_SURFACE` — and `settings.py` raises
  `ImproperlyConfigured` naming them (never the values). With valid values `manage.py check
  --deploy` passes except `security.W021` (HSTS preload is an operator decision);
  `marceline/tests/test_prod_settings.py` runs it in a subprocess. Don't weaken a rule to make a
  deploy work — fix the environment.
- **`marceline/public_surface.py` is the second wall** behind Caddy: with
  `DJANGO_PUBLIC_SURFACE=customer` the outermost middleware answers a bare 404 for everything but
  `/api/v1/app/…` and `/api/v1/health`, and for non-canonical paths (`..`, `//`, `%`, `\`). It is an
  allowlist mirrored in `infra/prod/Caddyfile` — change both together. `test_public_surface.py`
  walks every routed URL, so a new staff router is refused in production without anyone
  remembering to add it. `full` (the dev default) passes everything through.
- **No customer answer may be stored by a cache.** `marceline/no_store.py`
  (`CustomerNoStoreMiddleware`, above every middleware that answers, so it also covers the bundle
  answers of `customer_cache`) puts `Cache-Control: private, no-store` on every `/api/v1/app/…`
  response that sets none itself. A response with no `Cache-Control` is heuristically cacheable, and
  `Vary: Cookie` from the session middleware is only as good as the intermediary honouring it — the
  tenancy rule must not rest on that. `/api/v1/health` is left alone on purpose. The Next relay does
  the same for `/app/api/*` (`frontend/customer/lib/django.ts`).
- **No CORS, ever, for the customer API** (ADR 0028): the SPA is same-origin, so the absence of
  `Access-Control-*` headers is what makes browsers refuse other sites. Never install
  `django-cors-headers` or add such headers "for the frontend". Every unsafe customer endpoint
  checks CSRF (token + Origin); production trusts CSRF origins on its own hosts only.
  `marceline/tests/test_cross_origin.py` walks every `/api/v1/app` operation. CORS never stops curl —
  authentication, organisation scoping and the login throttle do.
- Production mail defaults to the SMTP backend (the console backend fails the deploy check).
- Sentry: `core/sentry.py`, off unless `SENTRY_DSN`; **EU data region only** (ingest host
  `*.de.sentry.io`, anything else refuses to start); `sentry-sdk` is a dependency, imported by name
  only once a DSN is set, and a DSN without the SDK refuses to start. `scrub_event` (`before_send`) keeps only the
  request method and path; no PII, local variables, bodies, cookies, headers, query strings,
  breadcrumb payloads or tracing. Never loosen these to debug something.

## Customer login (ADR 0023)
- **One `auth.User` for staff and customers**; the separation is authorisation, never a second
  identity table. `is_staff` opens the review API, a `Membership` opens the customer API, and one
  account may have both. Never add a customer account model — the tenancy boundary is membership
  plus the 404 rule, and it does not depend on table separation.
- `marceline/app/session.py` = `/api/v1/app/auth/{session,login,logout}`; `marceline/review/auth.py`
  = the staff login. Same Django session cookie, same CSRF dance (`csrftoken` cookie echoed as
  `X-CSRFToken`), same password hashing. **Never invent a token scheme.**
- **`accounts/login_throttle.py` is the only rate limiting for logins**, shared by both endpoints:
  5 failures per account and 20 per IP in a 15-minute window, then 15 minutes of 429 (with
  `Retry-After`) counted from the last failure. Settings `LOGIN_*`; `manage.py unlock_login --user
  NAME` lifts a lockout. Counters live in `accounts.LoginAttempt` (Postgres, so several gunicorn
  workers share them), keyed by an HMAC of the submitted username or the IP — **never store either
  in the clear**, and never key the bucket on a user row: an unknown username must lock out exactly
  like a real one.
- `X-Forwarded-For` is ignored unless `LOGIN_TRUSTED_PROXIES` says how many proxies are in front;
  the address is then taken that many entries from the right. Never trust the left-most entry.
- **A failed login says exactly one thing.** Unknown account, wrong password and inactive account
  share a 401 and a body, and every path hashes a password. Adding a distinguishable answer (a
  "this account is disabled" message, a faster refusal) is an account-enumeration hole.
- **No self-service signup** (#50) and **no password reset**: the operator creates accounts in the
  admin and sets passwords with `manage.py changepassword`. Both absences are asserted by a test
  and stated in the OpenAPI description; wiring either one is an ADR, not a patch.
- Cookie flags live in settings (`SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` default to
  `DJANGO_ENV=prod`, session cookie `HttpOnly`, both `SameSite=Lax`, CSRF cookie readable by the
  SPA on purpose).

## Cloud fingerprinting (issue #59, ADR 0044) — off by default

**Superseded 2026-09-19 (ADR 0044 amendment), kept verbatim because this file is the archive of the
condensed CLAUDE.md text (issue #79). The two paragraphs below were true until the streaming relay
was built; read them against the current rules that follow.**

> - **`/api/v1/app/uploads` is development-only** (`public_surface.ROLE_ONLY_PREFIXES["dev"]`), like
>   `/app/rights/proofs`: customer media never touches the public host, not in a spool, not in the
>   outbox, not in transit. ADR 0044 §9 designs the streaming relay that would change that; it is
>   not built, and production's credentials stay what they are. With `CLOUD_FINGERPRINTING_ENABLED`
>   off, or without a spool, every route answers 404 and `me.can_upload_media` is false.
> - **`marceline/app/uploads.py`**: `POST /uploads` (declare: sizes, title, cast, fps, the rights
>   declaration — everything refusable without the file is refused here), `POST /uploads/{id}/chunks/
>   {n}` …

- **`/api/v1/app/uploads` is development-only, or a streaming relay** (ADR 0044 §9 and its amendment
  of 2026-09-19). Customer media is never *stored* on the public host — no spool, no row, no outbox
  item, no temporary file — but since the amendment it may be *forwarded*: with
  `CLOUD_UPLOAD_RELAY_MODE` the prefix leaves `public_surface.dev_only_prefixes` and
  `marceline/uploads/relay.py` hands each 64 KiB read of the body to development's Tailscale-only
  `/internal/v1/relay/uploads…` as it reads it, then answers what development answered — reads
  included, because the row lives for minutes and a progress bar cannot come from a signed bundle.
  Its credential is a `ServiceToken` of scope `upload_relay` (never the read-only export token, never
  a database role), and it may only append to, complete or cancel a row it created
  (`MediaUpload.origin`). Without relay mode every route is the same bare 404 as before; with
  `CLOUD_FINGERPRINTING_ENABLED` off so is everything, and `me.can_upload_media` is false.
- **An upload carries the sole-performer declaration** (`marceline/app/upload_declaration.py`,
  version `upload-2026-09-19`): the uploader is the only person in the file **and** consents
  explicitly to the processing of that file. Two ticks, refused without either (`upload_declaration_
  required`), stored verbatim on the row with version, language and timestamp, re-checked at
  registration. A correction of the wording is a new version.
- **`marceline/app/uploads.py`**: `POST /uploads` (declare: sizes, title, cast, fps, the rights
  declaration and the upload declaration — everything refusable without the file is refused here), `POST /uploads/{id}/chunks/
  {n}` (raw bytes at their own offset; only the next chunk or the one just written), `POST
  /uploads/{id}/complete` (the customer's sha256 against the one the spool computed; a mismatch
  empties the spool and queues nothing), plus list/read/cancel. Caps: `CLOUD_UPLOAD_MAX_BYTES`,
  `CLOUD_UPLOAD_SPOOL_MAX_BYTES`, `CLOUD_UPLOAD_DAILY_BYTES_PER_ORG` and the existing
  `upload_throttle` (kind `original` — an upload is a registration attempt). Malformed, unknown and
  another organisation's `upl_…` are the same 404.
- **`register_fingerprint` is the one registration function** (`marceline/app/fingerprints.py`),
  split out of `register_original` for this: the browser endpoint, every outbox item and the upload
  worker all call it, so ownership, the rights declaration, the cast mapping, the generic 409
  `claim_needs_review` and the staff-only `RefusedClaim` are the same code, not the same rules. It
  re-checks the declaration's version, because a cloud upload's tick happened before the file was
  hashed.
- **`marceline/uploads/spool.py` owns the bytes and their deletion.** One directory per job named
  after its public id — never anything the customer sent — 0700/0600, in its own volume that only
  `web` and `fingerprint-worker` mount. `delete()` returns True only after a follow-up check says
  the directory is gone, and that return value is the **only** thing that writes
  `MediaUpload.bytes_deleted_at`. `reap()` removes everything past `CLOUD_UPLOAD_TTL_HOURS` without
  asking the database; it runs in the worker's poll *and* as the hourly `upload_spool` scheduler
  job, because a stopped worker must not be the only thing between a customer's file and its
  deletion.
- **`marceline/uploads/service.py` is one `try` whose `finally` deletes.** There is no branch that
  returns without it, and adding one is the bug `marceline/tests/test_no_retention.py` exists to
  catch (six guarantees, one test each). The decode itself is `matching.cloud_hash.hash_file` in a
  child process; this module holds the database connection, the child holds the file, never both.
- **`MediaUpload` holds no path, no blob and no file name.** The browser pre-fills the title from
  the file name and only the title travels — a file name can carry a legal name, a client or a
  shoot date; a title is the customer's own statement.
- **The fingerprinting progress is written by the parent, from two integers** (ADR 0044 amendment
  2026-09-18). The contained child reports `{"progress": {"frames", "total"}}` on the pipe it
  already has — it holds no database credential and this process never opens the file — and
  `uploads/service.ProgressWriter` stores the pair on `MediaUpload.frames_done` / `frames_total`
  (migration `marceline/0020_mediaupload_progress`). It is throttled: at most one update a second,
  and only when a whole percent or the denominator changed. The decoder reports **every** sampled
  moment, which for a 45-minute original at 1 fps is 2,700 reports and would otherwise be 2,700
  `UPDATE`s. `frames_total` stays NULL until the probe has read a duration — a null total is "not
  known yet", which the screen renders as an indeterminate bar rather than a denominator nobody
  has; it is an estimate even then, so the real count can exceed it and the bar clamps at 100 %.
  Claiming a job resets both counters, so a re-queued row never shows a stale bar.
- **`GET /api/v1/app/uploads/{id}` is the status the screen polls**, and the list endpoint carries
  the same fields. Beside the bytes it answers `frames_done`/`frames_total`, `bytes_deleted` and
  `reason` — a **closed set** the app has a sentence for in both languages (`unsupported_format`,
  `too_long`, `too_large`, `decode_failed`, `timed_out`, `quota`, `incomplete`, `needs_review`,
  `cancelled`, `internal`). `MediaUpload.result_code` is mapped onto it by `FAILURE_REASONS` in
  `marceline/app/uploads.py` and anything unmapped reads `internal`, so a code added anywhere can
  never reach a customer as an unworded English identifier. A test pins the answer's whole key set:
  no file name, no path, no decoder text. `bytes_deleted` is `bytes_deleted_at is not None` — the
  flag that *entitles* the screen to say the file has been deleted, and the only one.
