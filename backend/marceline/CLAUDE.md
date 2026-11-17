# Project package: targets, prospects, customer API, login, production surface, case files

What must not break here, and the single place each rule is answered. The full detail — field
lists, priority arithmetic, endpoint inventories and the narrative behind each rule — moved verbatim
to `docs/conventions/project-package.md` on 2026-09-18; the reasoning is in the ADR each line names.
The root `CLAUDE.md` hard rules and `backend/CLAUDE.md` apply here too. `marceline/capture/`,
`marceline/rights/`, `marceline/notices/` and `marceline/deletion/` have their own `CLAUDE.md`.

## Targeted indexing (issue #71, ADR 0029)
- **`marceline/targets.py` is the single answer to "whom do we hunt".** `targets()` = rights
  holders (primary creator of an original some organisation owns, via
  `marceline.app.ownership.owned_references`) at `SEED_PRIORITY`, co-performers of owned originals
  below them (`co_performer_priority`, pure, named constants), `catalog.WatchlistEntry` creators
  **only with `crawl` ticked**, and `purpose=prospect` entries last, capped per prospect per rolling
  24 h by `prospect_seed_plan`. The watchlist otherwise notes interest: no seeds, no active pages,
  breadcrumbs only. `crawl_seed` seeds only targets unless `--creator` names someone.
- **Deactivate, never delete — but not kept forever either** (ADR 0029 amendment 2026-09-17).
  `keep_rules` (linked, matched, clustered, seeded, safety) decides; `deactivate_untargeted`
  applies it and reactivates. **A non-clear page is never deactivated.** `apply_activity` scrubs a
  newly deactivated page (`index.retention.scrub_deactivated_pages`): title, tags and the performer
  credits in `raw_extraction` go, but the URL, every timestamp, the site, every hash and
  **`Page.creators`** stay — the `linked` rule needs that link to reactivate the page later.
- **The scrub is durable, and never touches a flagged page** (issue #148, ADR 0029 amendment
  2026-09-19). `store_parse_result` asks `index.retention.stays_scrubbed` and leaves those three
  fields alone while the page is still inactive for the `untargeted` reason — a re-crawl used to
  write them straight back. `scrub_deactivated_pages` itself filters `safety_status=clear`, so no
  caller can rewrite a flagged page's stored text. `scrub_inactive_pages` (dry run first) is the
  catch-up pass for pages deactivated before the scrub existed, and **`make status` prints the
  invariant** (`scrub_gap`): deactivated pages still holding text, which must be 0.
- `Page.deactivated_at` is the retention clock, set on deactivation and cleared on reactivation;
  `page_retention` (scheduler job guarded by `PAGE_RETENTION_ENABLED`) deletes the row
  `index.retention.PAGE_RETENTION_MONTHS` after that, `Media`/`FrameHash` cascading. Two guards: a
  page attached to an `open`/`reported` `SafetyIncident` is excluded up front, and a page a
  customer has drafted a real notice about is protected at the database level (`NoticeFinding.page`,
  `PROTECT`) — discovered only on the attempt.
- **Every page-side query in matching filters `media__page__active=True` / `active=True`**: frame
  loaders, mirror groups, SHA256/link evidence, common frames, leak suggestions, `claim_media`,
  capture triage. Loaders that take explicit page ids from human labels do not. **A new loader must
  do the same**, and incremental scans must keep treating `active_changed_at >= since` as new.
- Breadcrumbs (`index.SiteCreatorBreadcrumb` via `index/breadcrumbs.py`) are counts and dates, **no
  URLs**, and every counting query filters `page__active=True`, so a human browsing them sees no
  trace of who a now-deactivated page named.

## Prospect research (docs/prospect-research.md)
- `marceline/prospects.py` + CLI `prospect_report`: read-only, staff-only. **Attribution by names
  and PDQ clusters only — never faces or any biometric processing.** Safety-flagged pages are
  excluded from every count and the sample. Output carries page ids, domains and counts — **never
  media, titles or URLs**.
- The headline uses `strong` attribution only; **never build an outreach number from the loose
  total**, and say "approximately", never "at least" — the pipeline both misses pages and
  attributes wrongly.
- **The claim gate refuses by default** (2026-09-18):
  `claim_blockers` returns codes and `headline` prints them instead of a count. **There is no branch
  that formats the number without its "as of" date and measured precision** (a missing precision is
  itself a blocker) and a test walks every combination. The thresholds are named constants and are
  product policy, not statutory limits.
- `ResearchBasis` is an operator assertion per run, **never stored**. `render_briefing` is counts
  only — no page ids, no domains, no hosters. **Nothing here sends anything; there is no outreach
  automation and there must not be one.**

## Customer API and tenancy (ADR 0021)
- `accounts.Organisation` owns originals; `accounts.Membership` links a user to it. Professional and
  Enterprise are the same model — **never branch on `segment`**, it is billing and onboarding only.
  A user belongs to at most one organisation (database constraint), so a request's scope has
  exactly one answer.
- **Ownership is a column** (issue #76, ADR 0031): `marceline.OriginalOwnership`, one row per
  assigned original. It lives in the project app because `matching` may not import `accounts` —
  **never add a string FK from `Reference` to `accounts` instead**, it dodges the linter and breaks
  the contract. No row = unassigned = invisible to every customer.
- **`marceline/app/ownership.py` is the single definition and the only writer** (`owned_references`,
  `owned_originals`/`owned_original`, `owner_of`; `register`, `owner_for_new`, `reassign`,
  `with_twins`). **Membership never moves originals, and `Reference.uploaded_by` decides nothing** —
  it is audit data. Nothing else filters originals by user, membership or the ownership table.
  Registration chooses the owner explicitly and `reassign` is deliberate and logged.
- **A move costs the previous owner, deliberately** (ADR 0045, issue #85): `reassign` calls
  `marceline.deletion.handover`, which deletes the moved file's `LeakCase`/`CaseFinding` **in the
  same transaction** — the case hangs off the `Reference`, so otherwise the new owner would read the
  previous owner's `opened_at`, reopen history and per-finding stamps the moment the move commits;
  `sync_cases` rebuilds one. Everything else the previous owner holds about that file is **marked**
  (`OriginalRemoval`, reason `owner_changed`/`unassigned`) and deleted `CUSTOMER_DATA_RETENTION_DAYS`
  later, so an export can still be offered. `assign_originals` prints the plan **before** it acts —
  computed after the move it would print zeroes.
- **One customer original per file**: `owned_originals` groups an organisation's references by
  sha256 and the newest reference on a current pipeline is the representative. The `LeakCase` lives
  on the representative; `cases._case_of` moves it when a newer reference appears and merges cases
  two members grew, keeping the earliest stamps.
- The customer API is a **separate router** (`/api/v1/app/…`, `marceline/app/`), **never a widened
  review endpoint**; every customer view starts at `owned_originals` / `owned_original`.
- **Customers see public ids, never primary keys** (issue #77, ADR 0033, `core/public_ids.py`):
  `<prefix>_<32 hex>`, `public_id_field(prefix)` for a new kind. Look up originals only with
  `owned_original(organisation, public_id)`: malformed, integer, unknown and foreign ids are all the
  same 404 (`marceline/tests/test_app_public_ids.py`). **Never put an integer id into a customer
  schema or path**; staff/review APIs keep integers.
- **Cross-organisation access returns 404, never 403** — a 403 confirms the row exists, and the body
  is identical to an unknown id's (`marceline/tests/test_app_tenancy.py` asserts the equality).
  Staff are not customers: `is_staff` opens the review API, not another tenant's view.
- **Customer responses carry no media of any kind** (issue #53) and never mention a safety-flagged
  page — not even as a count (`marceline/app/safety.py`, ADR 0018).
- **No confidence insight for customers** (operator, 2026-09-17): a finding's evidence is only the
  code `evidence: fingerprint_match | no_fingerprint_match` (`originals.evidence_of`). **Never add a
  band, confidence, frame count or distance to a customer schema.**
- **Customers add originals** (issue #29, ADR 0019 + 0028 amendments): `POST
  /api/v1/app/fingerprints` imports through `from_json` → `import_fingerprint` →
  `ownership.register(via=CUSTOMER)` — **never duplicate that path**; the registration itself is
  `register_original(user, organisation, payload)`, which the endpoint and every outbox item call.
  A file the organisation already owns → 200, nothing imported; another organisation's or an
  unassigned file, or enough overlapping hashes → **one generic 409 `claim_needs_review` that names
  nobody**, persisted staff-only as `marceline.RefusedClaim` and queued at `GET /api/v1/review/claims`
  (ADR 0019: **never surfaced to any customer endpoint**). The rights declaration is versioned —
  **never edit a version's words in place, add a version**. On `MARCELINE_ROLE=prod` writes go
  through the outbox. **Codes only in responses.**
- **A customer original is a video *or* a photo collection** (issue #116, ADR 0053). One endpoint,
  one `register_fingerprint`: `_validated_shape` is the only place the two differ — the recipe tag
  (`BROWSER_PIPELINES`), the size cap (`MAX_POSITIONS` positions vs `MAX_IMAGES` = 5,000 images,
  both inside the route's 16 MiB body) and the refusal code that names it (`too_long` vs
  `too_many_images`, because the advice differs). Everything else — orientations, quality floor,
  rights holder, printable text, rate limit, idempotence, the generic 409, the cast mapping — is
  shared code, not merely shared rules. `CustomerOriginalOut.kind` sends the shape as a **fact**;
  never infer it from a null `duration_s`. The declaration's current version says *original*, not
  *video*, so it is true next to either (`rights_confirmation.json`, version `2026-09-18`).
  **No flag**: the change only widens what is accepted, and a video document takes the path it
  always took.
- **`catalog.Creator.needs_review`**: a name a customer typed that matched no creator by exact name
  (aliases never match). Excluded from `targets()`, `crawler.services.cached_creators` and discovery
  until staff mark it reviewed. Merging or aliasing moves every reference cast link and page link,
  unions aliases, logs the move (`marceline.CreatorMergeLog`) and deletes the source; **neither is
  automatic**. **A new creator-reading path that hunts, links or searches must exclude
  `needs_review` creators too.**

## Customer data deletion (ADR 0045, issue #85)
- `marceline/deletion/` — read `backend/marceline/deletion/CLAUDE.md` before changing it. Two
  scopes (an organisation whose pilot ended, one work of one organisation), one clock each, both
  set by a human and never inferred; deletion `CUSTOMER_DATA_RETENTION_DAYS` (30) later, from the
  scheduler's last job and only while `CUSTOMER_DATA_DELETION_ENABLED`. Pages, the index, billing,
  legal holds, another organisation's file and safety incidents are never deleted, and what is kept
  is reported by code. `manage.py end_pilot` / `remove_original` / `export_customer_data` /
  `customer_data_retention`, all with a dry run first. A customer downloads their own export
  directly too (`GET /api/v1/app/export`, ADR 0045 amendment 2026-09-18): the same
  `export_document`, rate-limited (`upload_throttle.Kind.EXPORT`, one per rolling hour) and
  development-only (`public_surface.ROLE_ONLY_PREFIXES`) like rights and notices.

## The creator's own data requests (ADR 0056, issue #133) — off by default
- `/api/v1/app/data-requests` (`marceline/app/data_requests.py`): *send me an export* (Art. 15,
  Art. 20) and *delete my data / end my pilot* (Art. 17), both from inside the app and **both on the
  public host** — which is the point, since `can_export_data` is false there and no deletion route
  existed anywhere. Rules and the model: `marceline/deletion/CLAUDE.md`.
- **A request records that they asked, and nothing else.** ADR 0045 is unchanged: `end_pilot` sets
  the date, `export_customer_data` renders the export, both by a person. Never wire a customer press
  to either.
- The paths name the **kind**, not a request id: at most one request per kind is open, so there is no
  id a caller could aim at another organisation's row, and a withdraw with nothing open answers the
  same 409 either way. `me.can_request_data` is what the shell asks before linking to `/app/data`.
- It reaches production like rights and notices — the `data-requests` bundle document and two outbox
  kinds — so `CUSTOMER_DATA_REQUESTS_ENABLED` is set on `web`, the public host **and** the `export`
  service that applies the writes.

## Retention on the research side (ADR 0048, issues #103, #104)
- `marceline/name_retention.py` + `crawler/retention.py`, one command `research_retention` with
  three passes (`queries`, `prospects`, `names`), dry run by default, and one scheduler job **last
  of all** behind `RESEARCH_RETENTION_ENABLED` — page retention and customer-data deletion both free
  names this pass then judges.
- **A prospect's research is forgotten `PROSPECT_RESEARCH_RETENTION_DAYS` (90) after the watchlist
  entry was added**: the entry, the `ProspectSampleCheck` verdicts, the `Page.creators` links and the
  breadcrumbs — the four things the privacy notice promises to forget. **A new verdict
  does not extend the clock**; a rolling clock keeps research alive exactly as long as we keep
  looking at it. A prospect who signed is kept (`customer_original`), and their name goes in the
  same run as their research if nothing else uses it.
- The keep rules and the guards are in `backend/catalog/CLAUDE.md`; the trade-off against ADR 0029's
  `linked` reactivation rule is decided in ADR 0048. **Never a name in the output or in a log.**

## The deletion journal and the restore replay (ADR 0057, DPIA R24, #137)
- **Every irreversible privacy action appends one line to `core.deletion_journal`**, at the single
  writer of its path — the thirteen hooks are listed in the ADR. A new deletion path gets a kind, a
  hook and a handler in the same change; a kind with no handler fails at import.
- **The line carries a kind, a stable key and a timestamp, and nothing else can go in it**: integers,
  64-hex digests, ISO dates and short enum tokens only (`_encode`). **Never a name, a title, a URL,
  an address or a note** — the replay works because the restored database still holds the name.
- Written from `transaction.on_commit`, so a rolled-back deletion leaves no entry; a failed append
  logs `CRITICAL` and never refuses the deletion.
- **`marceline/deletion_replay.py` + `manage.py reapply_deletions`** (dry run by default) re-applies
  each entry **through the function that deleted originally** — never a second implementation. It
  lives *beside* `marceline/deletion/`, not in it: that package imports nothing from `catalog`
  (ADR 0047) and a replay spans every path including the do-not-contact record.
- It **lists instead of acting** for a suppression lift, a `Creator` row the restore predates, a
  missing ADR 0045 mark (a date a human set) and anything an open safety case is attached to, and it
  refuses a page whose URL digest does not match. `DELETION_JOURNAL_DIR` reaches `web` and
  `scheduler` only (`marceline/tests/test_compose_env.py` pins both halves).

## Production settings and the public surface (ADR 0028)
- `DJANGO_ENV=prod` refuses to start unsafe: `core/deploy.py` (pure, `production_problems`) lists
  every problem and `settings.py` raises `ImproperlyConfigured` naming them, **never the values**.
  **Don't weaken a rule to make a deploy work — fix the environment**
  (`marceline/tests/test_prod_settings.py` runs `check --deploy` in a subprocess).
- **`marceline/public_surface.py` is the second wall** behind Caddy: with
  `DJANGO_PUBLIC_SURFACE=customer` the outermost middleware answers a bare 404 for everything but
  `/api/v1/app/…` and `/api/v1/health`, and for non-canonical paths. It is an allowlist mirrored in
  `infra/prod/Caddyfile` — **change both together**. `marceline/tests/test_public_surface.py` walks
  every routed URL, so a new staff router is refused in production without anyone remembering it.
- **No customer answer may be stored by a cache.** `marceline/no_store.py`
  (`CustomerNoStoreMiddleware`, above every middleware that answers, so it covers the
  `customer_cache` bundle answers too) puts `Cache-Control: private, no-store` on every
  `/api/v1/app/…` answer that sets none itself — a response without `Cache-Control` is
  heuristically cacheable, and **the tenancy rule must not rest on an intermediary honouring
  `Vary: Cookie`**. `/api/v1/health` is left alone on purpose; the Next relay does the same for
  `/app/api/*` (`frontend/customer/lib/django.ts`).
- **No CORS, ever, for the customer API**: the SPA is same-origin, so the absence of
  `Access-Control-*` headers is what makes browsers refuse other sites. **Never install
  `django-cors-headers` or add such headers "for the frontend".** Every unsafe customer endpoint
  checks CSRF (token + Origin). CORS never stops curl — authentication, organisation scoping and the
  login throttle do (`marceline/tests/test_cross_origin.py` walks every `/api/v1/app` operation).
- Sentry (`core/sentry.py`) is off unless `SENTRY_DSN`, **EU data region only** (anything else
  refuses to start), and `scrub_event` keeps only the request method and path — no PII, local
  variables, bodies, cookies, headers, query strings, breadcrumb payloads or tracing. **Never loosen
  these to debug something.**

## Customer login (ADR 0023)
- **One `auth.User` for staff and customers**; the separation is authorisation, never a second
  identity table. **Never add a customer account model** — the tenancy boundary is membership plus
  the 404 rule, and it does not depend on table separation.
- `marceline/app/session.py` and `marceline/review/auth.py` share the Django session cookie, the
  CSRF dance and the password hashing. **Never invent a token scheme.**
- **`accounts/login_throttle.py` is the only rate limiting for logins**, shared by both endpoints
  (`LOGIN_*` settings; `manage.py unlock_login` lifts a lockout). Counters live in
  `accounts.LoginAttempt` (Postgres, so several gunicorn workers share them), keyed by an HMAC of
  the submitted username or the IP — **never store either in the clear**, and **never key the bucket
  on a user row**: an unknown username must lock out exactly like a real one.
- `X-Forwarded-For` is ignored unless `LOGIN_TRUSTED_PROXIES` says how many proxies are in front;
  the address is then taken that many entries from the right. **Never trust the left-most entry.**
- **A failed login says exactly one thing.** Unknown account, wrong password and inactive account
  share a 401 and a body, and every path hashes a password. Adding a distinguishable answer (a
  "this account is disabled" message, a faster refusal) is an account-enumeration hole.
- **No self-service signup** (#50): the operator creates accounts, by invite since ADR 0052. The
  absence is asserted by a test and stated in the OpenAPI description; wiring it is an ADR, not a
  patch. **The password reset exists since ADR 0054** (issue #149) — that absence was lifted with a
  mechanism, not a patch, and its rules are below.
- **The mailed account actions** (ADR 0054, `ACCOUNT_EMAIL_FLOWS_ENABLED`, off):
  `/api/v1/app/auth/password-reset`, `…/password-reset/{token}`, `…/verify-email/{token}` are
  session-less customer surfaces; `…/verify-email` (the resend) is not. A **request answers 202
  `mail_requested` whatever the address is** — known, unknown, staff, inactive, transport down — and a
  link that does not work answers **one 404 `link_not_available`** for every reason. Never add a second
  sentence, status or faster path to either. The request route asks the flag **before it writes
  anything** (issue #153): with the flows off it is the same 202 and **no outbox row**. A **GET confirms nothing**: scanners fetch links, so the
  POST is the act. **Nobody is signed in by a reset.** On `MARCELINE_ROLE=prod` all four writes go
  into the outbox, the new password is hashed there and only the hash travels, and production deletes
  that account's sessions itself. Both token reads are **production-local** (its replicated token
  rows), like the invite's state; `GET /app/me`'s `email_verified` / `can_verify_email` gate nothing.
- **Onboarding by invite is the exception that proves it** (issue #115, ADR 0052, off by default):
  `/api/v1/app/invite/{token}/…` (`marceline/app/invites.py`) is the **only customer surface
  reachable without a session**, and it takes a token staff issued — there is still no endpoint a
  stranger can reach with an address of their choosing. Every failure answers **one identical 404**
  (`invite_not_available`): unknown, malformed, expired, revoked, redeemed, handed to the outbox
  and "switched off" alike, byte for byte. **Never add a second sentence, a second status or a
  faster path** — which of them applies is exactly what must not be learnable. The rules and the
  model are `backend/accounts/CLAUDE.md`.
- **It is public since 2026-09-19** (ADR 0052 §9, slice 2): the prefix has left
  `ROLE_ONLY_PREFIXES`, and on `MARCELINE_ROLE=prod` the last step creates nothing — it hands the
  redemption to `delivery.OutboxItem` (kind `invite_redeem`, both ids 0: there is no account yet)
  and answers **202**, or 503 while the outbox is off or paused. `GET /api/v1/app/invite/{token}` is
  **production-local**, answered from the invite rows `pull_accounts` replicates; both
  classification walks (`test_public_surface.py`, `test_cache_roundtrip.py`) list it as such. The
  accounts document carries only the token's **digest**, and only while the flag is on — the payload
  forbids extras, so an older production would refuse a whole document. **Deploy order: main, then
  the promotion, then the flag** (ADR 0052 amendment 2026-09-19).

## Account-control verification (ADR 0055) — off by default
- `/api/v1/app/verification` (`marceline/app/verification.py`): the creator claims the platform
  account whose content we protect, reads a one-time code, places it, and a staff member looks at the
  public profile **by hand**. Rules, model and the no-fetch guarantee: `marceline/rights/CLAUDE.md`.
- **It reaches production like rights and notices**, not like uploads: the status is a bundle
  document (`verification`, in `customer_cache.CACHED_ROUTES` and `export.LIST_DOCUMENTS`) and the
  two writes are outbox kinds. So it is **not** in `ROLE_ONLY_PREFIXES` — and
  `ACCOUNT_CONTROL_VERIFICATION_ENABLED` has to be set on both machines or the step is 404 on the
  public host.
- `me.can_verify_account` is what a screen asks before linking there;
  `InviteStateOut.account_step_next` is the same fact for the one page outside the session gate. Both
  are facts about the deployment, never about the caller.

## Cloud fingerprinting (issue #59, ADR 0044) — off by default
Endpoint inventory, caps and settings names: `docs/conventions/project-package.md`.
- **`/api/v1/app/uploads` is development-only, or a relay** (ADR 0044 §9, amendment 2026-09-19).
  Customer media is never **stored** on the public host: no spool, no row, no outbox item, no
  temporary file. With `CLOUD_UPLOAD_RELAY_MODE` the prefix leaves
  `public_surface.ROLE_ONLY_PREFIXES["dev"]` (`dev_only_prefixes`) and `marceline/uploads/relay.py`
  forwards each 64 KiB read of the body to development's Tailscale-only listener — **never touch
  `request.body` on that path**, it is what keeps Django's multipart handlers (the only thing that
  writes an upload to a temp file) out of it. Without relay mode every route is the bare 404 it
  always was, and with `CLOUD_FINGERPRINTING_ENABLED` off so is everything, `me.can_upload_media`
  included. Three guards that must agree: `core/deploy.cloud_upload_problems` (the process refuses
  to start), `dev_only_prefixes` (the routes), `infra/prod/check_config.py` (the compose file).
- **Development's side of the relay is `/internal/v1/relay/uploads…`** in `marceline/internal_api.py`,
  on the `export` surface, with its own token scope (`upload_relay` — never the read-only export
  token) and the rule that it may only append to, complete or cancel a row **it created**
  (`MediaUpload.origin`, a query and not a promise). It calls the same six operations in
  `app/uploads.py` the customer routes call; never a second implementation.
- **An upload needs the sole-performer declaration** (ADR 0044 amendment 2026-09-19,
  `app/upload_declaration.{py,json}`): the uploader is the only person in the file **and** consents
  explicitly under Art. 9(2)(a) GDPR — two ticks, refused without either, stored verbatim with
  version, language and timestamp, re-checked at registration like the rights declaration. It is the
  feature's whole Art. 9 basis, and a corrected wording is a **new version, never an edit**. A file with other
  performers is pointed at the local route, never silently accepted.
- **`register_fingerprint` (`marceline/app/fingerprints.py`) is the one registration function** —
  the browser endpoint, every outbox item and the upload worker all call it, so ownership, the
  rights declaration, the cast mapping, the generic 409 `claim_needs_review` and the staff-only
  `RefusedClaim` are the same code, not merely the same rules.
- **`marceline/uploads/spool.py` owns the bytes and their deletion.** `delete()` returns True only
  after a follow-up check says the directory is gone, and that return value is the **only** thing
  that writes `MediaUpload.bytes_deleted_at`. `reap()` runs in the worker's poll *and* as the hourly
  `upload_spool` scheduler job, because a stopped worker must not be the only thing between a
  customer's file and its deletion. The root is chmod'd back to 0700 if a volume mount left it at
  its default 0755, and reported through `problems()` rather than ignored when that chmod fails
  (first end-to-end trial, 2026-09-18).
- **"Inside the repository" is `core.checkout_paths.inside_checkout`, shared with the rights
  vault** (`marceline/rights/vault.py`) — the trial found both callers called *every* absolute
  path "inside the repository" in a container, where `BASE_DIR`'s parent is the filesystem root
  rather than a real checkout directory. **Never re-add a bare ancestor test against `BASE_DIR`'s
  parent** to either module; go through the shared helper.
- **`marceline/uploads/service.py` is one `try` whose `finally` deletes.** Never add a branch that
  returns without it — `marceline/tests/test_no_retention.py` exists to catch exactly that (six
  guarantees, one test each). The decode is `matching.cloud_hash.hash_file` in a child process:
  this module holds the database connection, the child holds the file, never both.
- **`MediaUpload` holds no path, no blob and no file name.** A file name can carry a legal name, a
  client or a shoot date; only the customer's own title travels.
- **The parent writes the progress, from two integers** (ADR 0044 amendment 2026-09-18).
  `uploads/service.ProgressWriter` stores what the child reports on `MediaUpload.frames_done` /
  `frames_total` (migration 0020): at most once a second, only on a whole percent. `frames_total`
  is **NULL until the probe read a duration** — "not known yet", an indeterminate bar; never invent
  a denominator and never turn either number into a time. **The throttle can drop the decode's own
  last update**, so `run_job` calls `ProgressWriter.flush()` once, unconditionally, after
  `hash_file` returns — a `done` job's bar must reach its own total (trial finding, 2026-09-18).
- **`GET /uploads/{id}` is what the screen polls**, and `reason` is a **closed set**
  (`FAILURE_REASONS` in `app/uploads.py`): `result_code` is mapped onto it and anything unmapped
  reads `internal`, so a new code can never reach a screen as an unworded string. `bytes_deleted` is
  `bytes_deleted_at is not None` and is the only thing that entitles the app to say the file was
  deleted. A status answer carries numbers and codes — never a file name, a path or decoder text.

## The customer case file (ADR 0025)
Full detail: `docs/conventions/matching.md`, where this section was archived from.
- `matching.LeakCase` (one per `Reference`) + `matching.CaseFinding` (one per page): **one case,
  many URLs**. A re-upload is a new finding on the **same** case, a fully removed case **reopens**,
  and there is no closed state — handled means "removed *and* not re-uploaded anywhere".
- **These tables store observations, never derivations**: only when a transition was *first* seen
  (`discovered_at`, `verified_at`, `removed_at`, `reappeared_at`), written once and never moved.
  `Page.availability_checked_at` is the **last** check and keeps moving — **never present it as a
  removal date**.
- `marceline/app/cases.py` owns the read model and `sync_cases` (it needs `marceline/app/safety.py`,
  and `matching` may not import `marceline`). With `CASE_SYNC_ON_READ` off (ADR 0028: the public
  cache must not write on read) views read `stored_cases` and `manage.py sync_cases` keeps them
  current. `_sync_case` takes a transaction-scoped advisory lock per original — **never
  `SELECT … FOR UPDATE` on the `Reference` row**, which matching holds for a whole run.
- The customer endpoints are scoped by organisation, answer a 404 byte-identical to an unknown
  id's, carry no media field, and **never list *or count* a safety-flagged page**.
- **Every value is a machine-readable code — the UI is bilingual and owns the words.** A German or
  English sentence in a customer payload is a bug.
- **Confirmed only, our step dated to the day** (ADR 0025 amendment 2026-09-17): customer views read
  through `originals.confirmed_findings_for`, so an unconfirmed page is no finding, count, case or
  removal. **Never add `discovered_at`, `verified_at`, `first_seen`, `opened_at` or any other stamp
  that would let a customer measure review latency.**
- **Say what cannot be known; never invent it**: the notice steps are `unavailable` and
  `not_available` names the absent facts as codes. **Never ship an empty placeholder object for
  them** — a placeholder later reads as data.
- A removals entry needs **both** `verified_at` and `removed_at`; `handled` also requires that
  nothing else of the same original is up. A page nobody confirmed that vanished is not a takedown.
- **A case file never changes owner** (ADR 0045, issue #85). It hangs off the `Reference`, which
  moves when `ownership.reassign` moves an original, so the move deletes the case and its findings
  in the same transaction and the next `sync_cases` rebuilds one for the new owner from the match
  rows. What does not survive a move is the previous owner's own record: `opened_at`, the reopen
  history and every per-finding `verified_at` / `removed_at` / `reappeared_at` stamp.
- A finding that stops being visible (rejected, or newly safety-flagged) is **deleted**, and the
  case goes with its last finding: an empty case with `reopen_count = 2` would itself tell a
  customer something is hidden from them.
