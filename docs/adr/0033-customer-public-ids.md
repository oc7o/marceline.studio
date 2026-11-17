# 0033 — Customers see opaque public ids, never primary keys

Status: accepted (2026-09-17)

Issue #77. Builds on ADR 0021 (tenancy and the 404 rule), ADR 0025 (case file), ADR 0028
(production cache) and ADR 0031 (one customer original per file).

## Context

The customer API (`/api/v1/app/…`) exposed database primary keys: `id` / `original_id` of originals
(a `matching.Reference` id), `page_id` of findings and removals (an `index.Page` id) and the
organisation `id` on `/me` — in payloads and in URLs (`/app/originals/41/copies/68211`).
Cross-organisation access already answered 404, so this was never an access hole, but sequential
ids leak volume: two uploads a week apart tell a customer how many originals everyone registered in
between, and a new finding's page id tells them how large our index is and how fast it grows.

Internal and staff APIs keep integer ids: staff see everything anyway, and the ids are what the
admin, the CLI and the logs speak.

## Decision

### A stored random id per customer-visible object

Every object a customer can name carries a second identifier, and only that one leaves through the
customer API:

| Object | Column | Prefix |
|---|---|---|
| organisation | `accounts.Organisation.public_id` | `org_` |
| page a finding sits on | `index.Page.public_id` | `page_` |
| customer original (one file as one organisation owns it) | `marceline.OriginalPublicId.public_id` | `orig_` |

**Format:** `<prefix>_<32 lowercase hex>`, the hex being a random UUIDv4 (122 random bits from the
OS CSPRNG, or Postgres's `gen_random_uuid()`). URL-safe, case-sensitive by construction (lowercase
only), self-describing: an organisation id pasted where an original id belongs is unknown, never a
different row.

**The helper is `core/public_ids.py`**, reusable by any app (`core` depends on nothing):
`public_id_field(prefix)` returns a unique, non-editable `CharField` with a Python default
(`generate`) **and** a database default (`RandomPublicId`, `db_default`); `is_valid(prefix, value)`
checks the shape; `missing(prefix)` is a well-formed id that is never generated (all zeros — a
UUIDv4 always has version nibble 4). Adopting it for a new kind is `public_id = public_id_field("…")`
and `make makemigrations`.

**Invoices (amendment 2026-09-17):** `billing.Invoice.public_id` (`inv_`) names a document in
`GET /api/v1/app/invoices` (`id`) and `/api/v1/app/invoices/{invoice_id}/pdf`; the view checks the
shape first, so malformed, integer, unknown, draft and foreign ids share the one 404. Issued
invoices are frozen by a `BEFORE UPDATE` trigger (ADR 0032), so a classic nullable-column-plus-
`UPDATE` backfill is impossible; `billing/0003` relies on the database default instead (the
`ALTER TABLE` rewrite fires no row triggers) and verifies every row afterwards with a `RunSQL`
check. `billing/tests/test_invoices.py` replays the migration's field and check against issued
invoices, a cancellation and a draft, and asserts the trigger then refuses a changed public id.

Why the database default as well: adding the column to a table with rows fills **every existing
row with its own id in the same `ALTER TABLE`** — no data migration, no batching script — and a
worker process started before the migration (the 2026-09-15 incident class) or a raw `INSERT` still
gets an id instead of failing a `NOT NULL`.

### An original's id belongs to the file, not to a reference

Since ADR 0031 a customer original is every reference of one sha256 that the organisation owns,
under a representative that changes when a re-fingerprint arrives and whose older twins the
operator may retire. An id on `Reference` (or on the representative's ownership row) would change
with either. `marceline.OriginalPublicId` is keyed by **(organisation, sha256)** instead, in the
project app for the same reason as ownership itself (`matching` may not import `accounts`):

- `ownership.register` and `ownership.reassign` write it when an owner is set; the read path
  (`public_ids_for`) creates any that are missing (`ignore_conflicts`), so an ownership row written
  some other way still gets one;
- a file moved to another organisation gets a **fresh** id there — the old id keeps naming the old
  organisation's row, which now owns nothing and answers 404 — and gets its old id back if it
  returns. Nothing about one organisation's ids says anything about another's;
- `ownership.owned_original(organisation, public_id)` is the only lookup: a malformed id, an integer,
  an unknown id, another organisation's and a file this organisation no longer owns are all `None`,
  and the views turn that into the one 404 body.
- Migration `marceline/0004` backfills one row per owned (organisation, sha256), counts printed.

`OwnedOriginal.pk` stays the representative's primary key for internal use (case rows, findings);
`OwnedOriginal.public_id` is what schemas render.

### What changes on the wire

- Paths: `/api/v1/app/originals/{original_id}` and `/api/v1/app/cases/{original_id}` take a string.
  An integer is no longer a 422 but the same 404 as any unknown id.
- Payloads: `CustomerOriginalOut.id`, every `original_id`, every `page_id`, and
  `OrganisationOut.id` are strings. Field names are unchanged.
- The customer UI routes `/app/originals/$originalId[/copies/$pageId]` take public ids, validate the
  shape before asking (`parsePublicId`), and render an old integer URL as not found. No redirects:
  there are no external customers yet.

### Production cache (ADR 0028)

- **Document keys carry the public ids**, exactly as the customer URL spells them
  (`originals/orig_…`, `cases/orig_…`, `*` rendered for `missing("orig")`). `delivery.store`
  accepts any public id shape in a key and refuses integers. On production a detail request whose id
  is not a well-formed original id is answered from the bundle's `*` document — the same 404
  development gives — without touching local tables.
- **Purges stay on primary keys.** They travel development → production only and never reach a
  customer. The bundle's `page_ids` therefore stays a list of page primary keys; the export
  collects the documents' `page_id` public ids and translates them back
  (`export._page_ids`), so a safety purge of page N still drops every bundle that shows page N
  (`test_cache_roundtrip`: purge by key drops the bundle that names the page by public id).
- The accounts document carries `organisations[].public_id` and production copies it, so `/me`
  names the organisation identically on both machines.

## Consequences

- Four migrations: `accounts/0003`, `index/0016` (rewrites `index_page`; run with workers stopped, as
  every migration here), `marceline/0003`, `marceline/0004`, and for invoices `billing/0003`. The
  page table gains a unique index on a 37-character column.
- **Deploy order:** development first, then production (`migrate`, then `pull_accounts` at once so
  `/me` shows development's organisation ids rather than the ones production's own migration
  generated). Until production runs the new code, it refuses the new bundles (`invalid_document_key`)
  and serves its stale ones, and it refuses the new accounts document (logins keep working from the
  last copy); the window is the deploy itself.
- Tests: `core/tests/test_public_ids.py` (shape, uniqueness, prefix rules),
  `marceline/tests/test_app_public_ids.py` (column defaults, the `ALTER TABLE` backfill, the
  original backfill, ids across reassignment, no integer id in any response, one 404 for malformed,
  integer, unknown and foreign ids), tenancy and cache round-trip tests on public ids, and frontend
  route tests for old and malformed URLs.
- Support: the admin searches organisations, pages and *Original public ids* by public id.

## Alternatives rejected

- **Encrypting or HMAC-ing the integer** (hashids, a keyed Feistel permutation). No migration, but
  the key becomes a forever secret (rotating it breaks every URL), a reversible scheme's security
  rests on a home-made construction, and it cannot express "one id for a group of references" —
  an original's id would still change with its representative.
- **UUID primary keys.** A table rewrite of every foreign key on live tables for the same effect.
- **An id on `Reference` or on `OriginalOwnership`.** Changes with the representative or disappears
  with a retired twin (see above).
- **Page ids per organisation (a project-app table of findings).** Would hide that two customers
  found the same page, but both already see its URL; not worth a second table written on every read.
- **Purges by public page id.** Would change `delivery`'s tables, tombstones and the purge
  envelope for no customer-visible gain: purges never leave our machines.
