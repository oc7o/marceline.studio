# 0031 — Originals are owned by an organisation column, kept in the project app

Status: accepted (2026-09-17)

Issue #76, review finding M2 (customer app review, 2026-09-17). Operator approved the column the
same day. Amends ADR 0021 ("Ownership resolves through `Reference.uploaded_by`, for now").

## Context

ADR 0021 resolved ownership as original → `Reference.uploaded_by` → that user's `Membership` → the
organisation, and promised that `marceline/app/ownership.py` would be the only code to change once
ownership became a column. The review found why "for now" had run out:

- **ownership followed the user, not the original.** Moving a member to another organisation moved
  every original they had uploaded, case file included; deleting the user or the membership
  orphaned them;
- **a staff account with a membership leaked.** A reviewer who uploads an original *for* a customer
  through the staff API becomes its `uploaded_by`; if that reviewer also holds a membership (a test
  account, say), their organisation owns the upload;
- **operator imports could not be assigned** without faking an uploader (`seed_test_customer` did
  exactly that), and an Enterprise organisation's originals depended on who happened to upload.

The issue asked for `Reference.organisation`. `Reference` lives in `matching`, `Organisation` in
`accounts`, and import-linter forbids `matching → accounts` (`backend/pyproject.toml`).

## Decision

### The column lives in the project app, as a one-to-one table

`marceline.OriginalOwnership` (`backend/marceline/models.py`):

| Field | |
|---|---|
| `reference` | one-to-one to `matching.Reference`, **primary key**, `CASCADE` |
| `organisation` | FK to `accounts.Organisation`, **`PROTECT`** — an organisation that owns originals cannot be deleted until they are reassigned |
| `assigned_via` | `upload` / `import` / `inherited` / `backfill` / `command` / `admin` |
| `assigned_by` | staff account behind the last assignment (`SET_NULL`); empty for the CLI and the backfill |
| `assigned_at` | when the current owner was set |

No row = unassigned: the original belongs to nobody and is invisible to every customer — exactly
what `uploaded_by` NULL meant before. It is a nullable column in all but storage: at most one
owner per original, enforced by the primary key.

Why here and not on `Reference`:

- **A string foreign key on `Reference` would pass the linter and break the contract.**
  `ForeignKey("accounts.Organisation")` is not a Python import, so import-linter would not see it,
  but `matching`'s schema and migrations would depend on `accounts` from then on. That is the
  silent loosening CLAUDE.md forbids.
- **Allowing `matching → accounts`** would be sound (no cycle — `accounts` depends only on `core`),
  but it hands the fingerprint engine a tenancy concept it never reads. Nothing in `matching` asks
  who owns an original; only the customer product does, and that already lives in the project
  package (`marceline/app`, `marceline/targets.py`, the case sync).
- **Moving `Organisation` into a lower app** (`catalog`, `core`) means renaming live tables and
  moving `Membership` with it, for no gain in the rule.
- **The project package may import every app**, and it already hosts the code that joins them
  (`scheduler`, `capture`, `targets`). It had no models; it now has this one table. No
  import-linter contract changes.

`Reference.ownership` (the reverse accessor) makes `Reference.objects.filter(
ownership__organisation=…)` a plain join, so `owned_references` stays one query.

### One module reads and writes it

`marceline/app/ownership.py` stays the single definition:

- reads: `owned_references(organisation)`, `owned_reference(organisation, id)`,
  `assigned_references()` (every original some organisation owns — the customer hosts for hoster
  lookup), `owner_of(id)`;
- `register(reference, organisation, via=, by=)` — the owner chosen when an original is created;
  refuses to overwrite an existing owner;
- `inherited_owner(sha256)` — the owner of earlier references of the same file; two owned twins
  that disagree raise instead of guessing;
- `reassign(references, organisation | None, via=, by=, dry_run=)` — the deliberate change; logs
  every row it moves (`marceline.ownership` logger: original, before, after, via, account id);
- `with_twins(ids)` — a reassignment always takes every reference of the same file (v0/v1
  re-fingerprints, ADR 0022), so one video never belongs to two organisations.

**Membership changes never move originals**, and `uploaded_by` decides nothing: it stays the audit
of who registered the file. A user with no membership still owns nothing, staff included, and the
404-not-403 rule is unchanged.

### When an owner is set

- **Staff upload API** (`POST /api/v1/originals`): `organisation_id` is a **required** field that
  may be `null`. The uploader has to choose — an organisation, or explicitly "unassigned" — and the
  staff SPA lists organisations from `GET /api/v1/originals/organisations`. An unknown id is a 400.
- **CLI** (`add_reference`, `import_fingerprints`): `--organisation NAME` (case-insensitive exact
  name). Without it, the owner is inherited from an earlier reference of the same sha256 — the same
  rule `import_fingerprints` already applies to the cast, and printed the same way (`owner from
  reference N`). Otherwise the original stays unassigned. A `--organisation` that contradicts the
  owner of an earlier reference of the file is refused per file.
  Both commands moved from `matching/management/commands/` to `marceline/management/commands/`:
  they now wire `matching` and ownership together, which only the project package may do. Their
  names, flags and output are otherwise unchanged; `matching.references` still does the import.
- **Deliberate change**: `manage.py assign_originals --organisation NAME | --unassign
  (--reference ID … | --creator NAME …) [--dry-run]`, or the admin (*Marceline → Original
  ownerships*), whose change form requires ticking a confirmation before an owner moves.
  `seed_test_customer --reference` now assigns the column instead of faking an uploader.

### One customer original per file

Added the same day, after the operator saw 41 originals for 39 videos on the test account: a
re-fingerprinted video is several references with one sha256 (v0/v1 grids, 1 and 2 fps). Ownership
already moves such twins together (`with_twins`); the customer read model now treats them as one
original too. `owned_originals(organisation)` groups the owned references by sha256; the newest
reference on a current pipeline (not in `SUPERSEDED_PIPELINES`) is the representative — its id,
title and metadata are what a customer sees — and `owned_original` answers only that id. Findings
are merged across members and the case file lives on the representative (ADR 0025 amendment of
2026-09-17). No migration: the first sync after deploy moves and merges the existing cases.
Superseded for the id by ADR 0033: customers name an original by the public id of its file
(`marceline.OriginalPublicId`), not by the representative's primary key.

### Backfill

`marceline/migrations/0002_backfill_original_ownership` writes today's rule down once: every
reference whose uploader has a membership gets a row for that organisation (`assigned_via =
backfill`); references with no uploader, or an uploader without a membership, stay unassigned;
references that already have a row are skipped. Primary-key order, idempotent, counts printed
(`assigned`, `already owned`, `no uploader`, `uploader without membership`). On 2026-09-17 the
expectation is 41 assigned — the 39 v1 originals and 2 v0 twins of the test organisation.
Reversing deletes only `backfill` rows.

## Consequences

- **Two migrations ship** (`marceline/0001_initial`, `0002_backfill_original_ownership`). Apply from
  the main checkout (`make migrate`), check the printed counts, restart the workers.
- `frontend/src/api/schema.d.ts` changes with the upload schema; the staff upload panel gains an
  organisation picker whose default is "choose…", not a silent value.
- ADR 0028's export (proposed) reads ownership through the same module, so nothing there changes;
  a reassignment is a data change like any other and needs the same purge.
- Existing tests that built "owned" originals by setting `uploaded_by` now also create the row
  (`marceline/tests/app_helpers.make_reference`, `own`).

## Alternatives rejected

- **`Reference.organisation` as a string FK in `matching`.** See above: invisible to the linter,
  a real dependency all the same.
- **A contract change `matching → accounts`.** Sound, but it puts customer tenancy into the
  matching engine for one column no matching code reads.
- **Moving `Organisation`/`Membership` into `catalog` or `core`.** Table renames on live data and a
  worse home for accounts, to save one join.
- **Keeping the membership rule and forbidding membership moves.** It fixes neither the staff leak
  nor operator imports, and Enterprise seats (phase 3) would reopen it.
- **An ownership history table.** Reassignment is a rare, operator-only action; the log line, the
  admin's own change log and `assigned_via`/`assigned_by`/`assigned_at` are enough until customers
  can transfer originals themselves.
