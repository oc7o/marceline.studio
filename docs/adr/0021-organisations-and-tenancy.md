# 0021 — Organisations, tenancy and the 404 rule

Status: accepted (2026-09-16)

Implements the accounts section of `docs/customer-ui-plan.md` (issue #32), phase 1. Backend and
API only — the customer UI is deferred by the operator.

## Context

Everything built so far has one audience: the operator, through a staff-only moderation API. The
paying audience is a creator or a studio, and it asks a different question — "is *my* work leaked,
where, and what is being done about it". That needs an owner for an original, and a guarantee that
one customer never sees another's anything.

The guarantee is the hard part. A leak record is, by its nature, a statement about someone's
private material; a customer product that confuses two accounts does not have a bug, it has an
incident. So the tenancy rule is the change, and the endpoints are what exercises it.

## Decision

### Two segments, one model

`accounts.Organisation` (name, `segment`, `created_at`) owns originals. `accounts.Membership`
links a `auth.User` to it with a role (`owner` / `member`).

**Professional** — an independent creator — is an organisation with exactly one user.
**Enterprise** — a studio or agency — is one with several. The segment is a billing and onboarding
fact and is **never** read to decide access; nothing in the code branches on it. A solo creator who
hires an assistant changes plan, not data model, which is the whole reason for refusing the
shortcut of hanging originals off a `User`.

A user belongs to **at most one** organisation, enforced in the database
(`membership_one_organisation_per_user`). "Which organisation is this request scoped to" then has
exactly one answer and no request has to choose one. An agency employee serving several customers
needs an active-organisation concept, which is a product decision (invitations and seats, phase 3);
until it is taken, the database refuses the ambiguity rather than letting a view guess.

`role` carries no permissions yet: an owner and a member see the same originals. It records who the
account holder is and gives seats somewhere to land.

The models live in the `accounts` app. Its migrations are then clear of `index` and `matching`,
which are being changed in parallel, and `accounts` keeps its import contract (it depends only on
`core`).

### Ownership resolves through `Reference.uploaded_by`, for now

*(Superseded 2026-09-17 by ADR 0031: ownership is an organisation column. See the amendment below.)*

`matching.Reference` gets **no** organisation column in this change. Ownership is:

> original → `Reference.uploaded_by` (the account that registered it, issue #29) → that user's
> `Membership` → the `Organisation`.

Written once, in `marceline/app/ownership.py`, as `owned_references(organisation)` /
`owned_reference(organisation, id)`. Nothing else filters originals by user or membership, so when
ownership does become a column on `Reference`, that module is the only code that changes.

Why not the column now: the 25 existing references were imported by the operator and have no owner
at all, so a nullable FK would need a backfill nobody can compute — and `matching` is being
migrated in parallel by another agent, where a second migration would collide. The indirection
costs one join and buys the decision being reversible.

Consequences of the indirection, all deliberate:

- an original with `uploaded_by` NULL (an operator or CLI import) belongs to **nobody** and is
  invisible to every customer until it is assigned by hand. Legacy material is never auto-claimed;
- an upload remains a **claim, not proof of ownership** (ADR 0019). Tenancy scoping decides who may
  *see* a row, never who holds the rights to the material;
- if a user is ever moved between organisations, their originals move with them. For a Professional
  account (one user) that is the same thing as moving the account; for Enterprise it is a reason to
  put the column on `Reference` before seats ship.

The helper lives in the project package, not in `accounts`, because import-linter forbids
`accounts → matching`. The rule is four lines; the boundary is worth more than the tidiness.

### The customer API is a separate router, and it answers 404

`/api/v1/app/…` (`marceline/app/`), registered next to — never merged into — the review API.
Shared schemas would be fine; a shared queryset is how tenant leaks happen.

Phase 1 endpoints, all read-only, session-authenticated:

| Endpoint | Answers |
|---|---|
| `GET /api/v1/app/me` | the signed-in user and the organisation their requests are scoped to |
| `GET /api/v1/app/originals` | every original the organisation owns, with its state and counts |
| `GET /api/v1/app/originals/{id}` | one original and every page it was found on |

**Cross-organisation access returns 404, never 403.** A 403 answers "that row is real, it is just
not yours", which is exactly the fact worth hiding. The refusal is byte-identical to the refusal
for an id that does not exist, and a test asserts that equality rather than the status code alone.

Staff get no special treatment here: `is_staff` opens the review API, not somebody else's account.
An authenticated user with no organisation is a valid caller who owns nothing — an empty list and
`organisation: null`, not a 403.

### What a customer may see in phase 1

- **A safety-flagged page is never listed, described or counted** (ADR 0011, ADR 0018). Not "one
  result hidden": a count would itself say a hidden page exists. A page that merely matched a
  safety term is excluded even if a reviewer later cleared it — the same conservative rule the
  capture triage uses.
- **No pirate imagery at all** (#53). Enforced by absence: no schema in
  `marceline/app` has a media field, and nothing there reads `index.Media`. Evidence is site, URL
  and dates — text a rights holder can verify with an explicit click.
- **No confidence percentages.** The match's band (`high` / `review` / `low`) is passed through; a
  number invites an argument about the number.
- A match nobody has looked at whose band is `low` is not shown at all. It is fingerprint noise,
  and "your video might be on this site, two frames matched" is worse than silence. Any human
  decision (a non-rejected `ReferenceLeak`) overrides that rule.
- The overview's three states (`nothing_found` / `found_online` / `found_removed`) are decided by
  **confirmed** findings only; unconfirmed matches are reported separately as review workload. An
  unreviewed fingerprint hit is not an answer.

### Test data

`manage.py seed_test_customer [--user] [--organisation] [--segment] [--reference ID …] [--dry-run]`
puts `test-customer@marceline.studio` into a Professional organisation as owner and assigns the
originals named by id. It is idempotent and timid: it never moves a user between organisations,
never reassigns an original another account already claims, and touches nothing that was not named
on the command line. There is no default set of references — which of the operator's imports a test
account owns is an operator decision (open question 3 of the plan), not a constant in code.

## Consequences

- **A migration ships with this change** (`accounts/0001_initial`: `Organisation`, `Membership`).
  Apply it from the main checkout (`make migrate`) and restart the workers.
- ~~**There is no customer login endpoint yet.**~~ **Closed by ADR 0023** (2026-09-16):
  `POST /api/v1/app/auth/login` signs in an account with a membership, on the same session
  mechanism as the staff login and with rate limiting and lockout on both. One `auth.User` serves
  staff and customers — the separation is authorisation (`is_staff` versus membership), never a
  second identity table. There is still no self-service signup (#50) and no password reset; the
  operator creates accounts and sets passwords by hand.
- The review API is untouched and still staff-only. Two routers now read the same tables with
  different querysets, which is the intended cost.
- `frontend/src/api/schema.d.ts` regeneration needs `node_modules`, which a git worktree does not
  have. `backend/openapi.json` is regenerated here; run `make frontend-api` after merging.

## Alternatives rejected

- **An organisation FK on `Reference` now.** Cleaner to read, but it collides with the parallel
  `matching` migration and needs a backfill for 25 unowned imports. The indirection is one join and
  one module.
- **A `Customer` model instead of an organisation with one user.** It makes Professional a special
  case in every query and turns a plan upgrade into a data migration — the operator's decision was
  explicitly the opposite.
- **403 for another tenant's resource.** Friendlier to debug, and it confirms the resource exists.
- **Reusing the review router with a tenant filter.** One forgotten `.filter(...)` is a breach; the
  separate router makes the scope a property of the module rather than of each view.
- **Showing thumbnails of the pirated copy.** Blocked on #53, and the evidence is convincing
  without imagery.

## Amendment 2026-09-17: ownership is a column (issue #76, ADR 0031)

The indirection through `Reference.uploaded_by` is gone. Review finding M2 showed its cost: moving
or deleting a member moved or orphaned their originals, a staff uploader holding a membership
owned what it registered for a customer, and operator imports could only be assigned by faking an
uploader. Ownership is now `marceline.OriginalOwnership` (one row per assigned original, FK to
`accounts.Organisation` with `PROTECT`), kept in the project app because `matching` may not import
`accounts` — ADR 0031 has the reasoning and the alternatives.

What changes in this ADR:

- *"Ownership resolves through `Reference.uploaded_by`, for now"* is superseded. `ownership.py`
  still is the single definition — as promised, it is the module that changed — and it now also
  owns the writes (`register`, `reassign`). `uploaded_by` is audit data only.
- *"If a user is ever moved between organisations, their originals move with them"* no longer
  holds: membership changes never move originals.
- An unassigned original (no row) belongs to nobody, as `uploaded_by` NULL did. It is assigned
  deliberately with `manage.py assign_originals` or in the admin with confirmation; uploads and
  imports choose an organisation explicitly (`organisation_id` / `--organisation`) or inherit it
  from an earlier reference of the same file.
- `seed_test_customer --reference` assigns the column; it still refuses an original another
  organisation owns and still never moves a user.
- The migration backfilled the old rule once (`marceline/0002`), so no customer view changed on
  the day.

## Amendment 2026-09-18: the *Unternehmer* evidence beside the organisation (issue #92)

The pilot is B2B-only and EU-only (operator decision, issue #95 item 7). A tick-box
alone does not establish that a customer acts as a business. What keeps the scope is the
objective evidence we looked at and recorded at signature.

What this adds to this ADR:

- **`accounts.BusinessStatus`**, one-to-one with `Organisation`: the kind of evidence from a closed
  list, a note saying what was checked, the VAT ID, the country, who looked and when, and the
  customer's own declaration with its verbatim wording and version (`accounts/declarations.py`, the
  versioning rule of `marceline/rights/declarations.py` — a new wording is a new version).
- It lives in `accounts`, not in `marceline/rights/`, for two reasons: it is a fact about the
  *contracting party* rather than about a work, and the guard that reads it sits on `Membership`,
  which `accounts` owns. `accounts → core` still holds; nothing new is imported.
- **A `Membership` is the grant of customer access** (`can_add_originals` in
  `marceline/app/account.py`), so that is where the guard sits. `ONBOARDING_BUSINESS_STATUS_GUARD`
  defaults to `warn` — the admin shows a message and saves — and `refuse` makes the membership form
  reject it. The seeding commands never call `full_clean`, so `seed_test_customer` and
  `seed_demo_dataset` keep working either way, and a sandbox organisation (ADR 0035) is never asked.
- **No identity documents**, the same line ADR 0034 draws for `RightsClaimant`: staff record *that*
  a document was seen and enough to find it again, never its contents and never a copy. The VAT ID
  has its own field because it identifies an undertaking rather than a person and is publicly
  verifiable — which is also what keeps person-level document numbers out of the free-text note.
- **Nothing customer-facing changes**: no screen, no API field, no public endpoint. Accounts are
  created by hand (there is no checkout and no web form).
- A country outside the EU is **recorded and flagged, never refused**: complete evidence of a party
  outside the pilot's scope is a commercial question, not a gap in the file.
