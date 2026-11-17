# Production read-through cache

Moved verbatim out of `backend/CLAUDE.md` on 2026-09-18 so that file stays small. The root `CLAUDE.md` hard
rules and `backend/CLAUDE.md` apply here too.

## Production read-through cache (ADR 0028)
- **Two roles, one codebase.** `MARCELINE_ROLE=dev|prod` (default follows `DJANGO_ENV`). Surfaces:
  `full`/`export` on dev, `customer`/`internal` on prod; a mismatch refuses to start
  (`core/deploy.role_problems`). `public_surface.py` also 404s `/internal/v1/export/…` on prod and
  `/internal/v1/cache/…` on dev. The internal API is its own `NinjaAPI` (`marceline/internal_api.py`,
  no docs, not in `openapi.json`).
- **`delivery` (core only) moves and stores signed bytes; it never decides what a customer sees.**
  `envelope.py` (Ed25519, `format` + `schema_version` inside the signature, domain-separated),
  `tokens.py` (SHA-256 digests only, **two scopes**: `export` is read-only; `upload_relay` is cloud
  fingerprinting's streaming relay (ADR 0044 §9) and reaches `/internal/v1/relay/…` alone — neither
  can do the other's work, and `create_service_token --scope` picks one), `store.py` (TTL 15 min / stale 72 h are constants,
  per-organisation advisory lock, tombstones), `purge.py` (±5 min window, single-use nonce),
  `client.py` (httpx, `transport` hook for tests, never logs a URL — error codes only).
- **Bundles are rendered by the customer views themselves** (`marceline/export.py` resolves each
  path and calls the view for a member of the organisation). Never serialize a customer document
  a second way: byte identity with the live endpoint is tested (`test_cache_roundtrip.py`). A new
  customer read endpoint with organisation data must be added to `export.document_keys`,
  `store.DOCUMENT_KEY` and `customer_cache.CACHED_ROUTES` together, or prod would answer it from
  its empty local tables. Detail keys are **public ids** as the URL spells them (`cases/orig_…`,
  ADR 0033; `DOCUMENT_KEY` refuses integers); on prod a malformed id is served the `*` 404.
  Purges and the bundle's `page_ids` stay **primary keys** (they never reach a customer):
  `export._page_ids` translates the documents' `page_id` public ids back, so keep the field name
  `page_id` on anything that shows a page, or safety purges stop finding the bundle.
- **Notice drafts ride the same rails (ADR 0046)**, with three things worth knowing before touching
  them. A document may carry a **409** as well as 200/404 (`store.DocumentIn`): a notice download
  whose draft is not approved, or already closed, has no file, and production must answer that
  refusal rather than a 404 that would read as "no such draft". The **notice download is served by
  its own view**, not by `CustomerCacheMiddleware` — serving it records an `exported` event, and
  `delivery` never decides what a write means. And `export.render` marks its synthetic request
  (`bundle_render`), so a view can tell "fill the cache" from "a customer asked": the download
  formats without logging then, and never reads the cache.
- On prod, `customer_cache.CustomerCacheMiddleware` (innermost) serves those routes from the cache
  with `X-Data-As-Of`, or 503 `data_temporarily_unavailable` — never an empty list. `me` and
  `auth/*` stay local; accounts come from `pull_accounts` (`marceline/accounts_replica.py`, no staff
  flags ever), and `coverage.monitored_sources` reads the replicated bucket.
- **Purges** (`marceline/cache_purge.py`): page leaves safety-clear → `delete` (signals in
  `marceline/apps.py`; bulk updates via `send_cache_purges --flagged-within-minutes`); review
  decisions → `expire` of owning organisations (through `ownership.owned_references` only); a
  customer deletion (ADR 0045) → `delete` of that organisation, so production drops its bundles
  instead of serving them stale, while the login goes with the next `pull_accounts`. Written
  to `PurgeOutbox` in the causing transaction, sent on commit when this process has
  `CACHE_PURGE_BASE_URL`, otherwise only recorded (`CACHE_PURGE_OUTBOX`).
- **Customer writes on production (ADR 0028 amendment "The production write path").** Production
  never writes customer data into its own model tables: a customer write is validated there with
  the endpoint's own checks, stored as `delivery.OutboxItem` (`delivery/outbox.py`: `enqueue`,
  `paused`, signed batches, `apply_ack`, `housekeeping`) and answered 202; development pulls it
  (`delivery/inbox.py` `run_once`, CLI `pull_customer_writes`, cron in `export`) and applies it
  with `marceline/customer_writes.py` `HANDLERS`, which call the **same function the development
  endpoint runs** — never a second implementation. Keys: development signs pulls/acks with
  `CACHE_SIGNING_KEY_PATH`; production signs batches with its own `OUTBOX_SIGNING_KEY_PATH`
  (`internal` only, never `web`); each side verifies the other's public key. Apply is once per item
  (`InboxItem` in the same transaction), in order per organisation, quarantined after
  `MAX_APPLY_ATTEMPTS` with the exception *class* as reason (never its message). The ack empties the
  payload and expires the bundle; `store_bundle` stores a bundle older than an applied write as
  expired. Customers follow it in `GET /app/fingerprints/submissions` (production-local, not in the
  bundle). **A new customer write endpoint** must get a kind (`OutboxItem.Kind`), a 202 path on prod
  and a handler, or stay dev-only in `ROLE_ONLY_PREFIXES`; rights (ADR 0034) and notice drafts
  (ADR 0046) are wired, proof documents and the notice sandbox send stay dev-only. Deploy
  development before production (unknown kinds quarantine).
- **Account-control verification is wired the same way** (ADR 0055, off by default):
  `verification_claim` and `verification_placed`, the `verification` bundle document, no
  `ROLE_ONLY_PREFIXES` entry. The **code is generated on development**, never here: it has to exist
  where the staff member who compares it by eye will read it, so production's 202 carries no code and
  the creator reads it from the next bundle. `verification_placed` names the claim by its public id
  and development resolves it, because production holds no challenge rows.
- **The creator's own data requests are wired the same way** (ADR 0056, issue #133, off by
  default): `data_request_open` and `data_request_withdraw`, the `data-requests` bundle document, no
  `ROLE_ONLY_PREFIXES` entry. The row lives on development because that is where a person answers
  it, so the **dedupe key is the request kind** (a double press is one write) and the typed deletion
  word travels with the payload and is checked again on apply. The public host is the *reason* the
  feature exists: `can_export_data` is false there, so these two requests are the only way a
  creator who never sees development can exercise Art. 15/20 or Art. 17. The switch must reach
  development's `export` service too — that is where `pull_customer_writes` runs, and without it a
  request is pulled and then refused with nobody the wiser.
- **`invite_redeem` is the one write from before an account exists** (ADR 0052 §9, slice 2):
  `organisation_id` and `user_id` are both **0**, so `_member` does not apply and no customer's
  submissions list shows it; the payload names the invite by its token **digest** and carries the
  password **hash** production made. `accounts.invites.apply_redemption` re-judges it and ends in
  `complete()`. An invite already redeemed settles `done` / `already_redeemed` — not a refusal.
- **The four `password_reset_*` / `email_verify*` kinds are the mailed account actions** (ADR 0054):
  three of them carry no account (`organisation_id`/`user_id` 0 — a reset is asked for before anybody
  is signed in) and the fourth is a signed-in customer's own resend. `password_reset_set` carries the
  new password's **hash**, never the password; `password_reset_request` carries the address the caller
  typed, which development alone may act on. The public host holds **no mail transport** and
  `check_config.py` refuses one.
- **The accounts document has an `invites` section** since the same change: per open invite the
  token digest, the expiry, the address and the destination organisation, so the public host can
  refuse a bad token with no way to ask development anything. The key is **absent** while
  `ONBOARDING_INVITES_ENABLED` is off, because `AccountsPayload` forbids extra fields and an older
  production would otherwise refuse the whole document — which is why the promotion goes **before**
  the flag, the one place the usual "development first" order is reversed. ADR 0054's `action_tokens`
  section and its `email_verified_at` per user follow exactly the same rule, under their own flag.
- Tests: throwaway keys from `delivery/tests/helpers.py`, fake clock via `now=` /
  `customer_cache.clock`; `MARCELINE_ROLE`, `CACHE_*` and `OUTBOX_*` are pinned in
  `OPERATIONAL_DEFAULTS`. The write path end to end: `marceline/tests/test_customer_writes.py`;
  the Caddyfile's body limit and internal paths: `test_prod_edge_config.py`.
