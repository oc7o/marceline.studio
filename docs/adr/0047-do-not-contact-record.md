# 0047 — The do-not-contact record: a keyed hash, kept for good

Status: accepted (2026-09-18)

Operator decision of 2026-09-18. Process: `docs/do-not-contact.md`.

## Context

Anyone we write to may answer with one sentence, no reason given, and that ends the contact for
good. Honouring that needs a record rather than a memory: without one we cannot reliably recognise
an identifier we must not write to again. The record is minimal and has one new, narrow purpose —
"honour no-contact requests" — and it preserves nothing else about the person:

    normalised contact identifier / do-not-contact / date / scope

Before this change the record was a note on paper, which stops working as soon as there is more
than a handful of entries.

## Decision

### One row per identifier, holding a keyed hash of it and nothing that names anybody

`catalog.DoNotContactRecord`:

| Field | Why |
|---|---|
| `identifier_hash` | HMAC-SHA256 over `kind:normalised identifier` with `DO_NOT_CONTACT_HMAC_KEY`, hex, unique. **The identifier itself is never stored.** |
| `identifier_kind` | `email`, `postal`, `organisation`, `handle`, `phone`. Part of the hashed message, so a record of one kind can never block another. |
| `received_on`, `channel`, `recorded_by` | The date, how the "no" arrived, who wrote it down — Art. 5(2) accountability, and nothing more. |
| `creator` | Optional `SET_NULL` link to a prospect `Creator` row *if one exists*. Never required, and its deletion leaves the objection standing. |
| `note` | One line for staff. The help text says: no names, no contact details, no quote from the message. |

One person can have several rows, one per identifier they gave us. That is the intended shape: the
record can only recognise what somebody hands it, so a "no" given by letter blocks the address, and
the same person's email address is blocked once it too is recorded.

**Why a hash and not the identifier.** The purpose is narrow — recognise an identifier somebody is
about to write to — and a hash serves it completely while making the table useless for anything
else. It cannot produce an address, a name or a list of people, it is worthless if copied, and it
keeps the promise of "we deleted everything else about you" literally true. Pseudonymisation
reduces harm while the data stay personal data; we take the reduction and make no
claim beyond it.

**Why keyed and not a plain digest.** A plain SHA-256 of an email address is an identifier anybody
can recompute and look up: with a list of candidate addresses the table becomes readable again. The
HMAC key makes the digests meaningless outside this installation.

### Normalisation is conservative, and the failure it prefers is "record both spellings"

`catalog/do_not_contact.py` is pure and is the only place spellings are folded: email case only (no
dot- or plus-folding — that is one provider's convention and would merge two mailboxes); German
umlauts and the `str.`/`straße` abbreviation in addresses; conjunctions dropped but legal forms kept
in company names; a handle always carrying its platform; `+49`/`0049`/`(0)` collapsing in phone
numbers while a **national** spelling stays its own identifier, because resolving it means guessing
a country.

Two spellings of one identifier must collide; two people must never. Where those pull apart the rule
keeps them apart, and the staff screen says to record every spelling that was given. A missed block
is the failure that matters; a wrongly blocked stranger is a second harm and also silent.

### The check is a command, and it answers by position

`manage.py may_contact --kind email …` takes identifiers as arguments or on stdin and prints
`1: allowed` / `2: blocked` — never the identifier, in the output or in the error. It exits non-zero
when anything is blocked **or unusable**, because an unchecked identifier is not an allowed one, so
it can guard a script or a session that is about to write letters.

Recording happens on the staff screen (admin), where the identifier is a write-only form field: it
is hashed in `clean()` and never reaches a model field, a log line or the admin's own change log.
The record is not deletable there.

### The key is never rotated

Every stored hash is keyed with `DO_NOT_CONTACT_HMAC_KEY`, and the hashes are one-way. A new key
does not invalidate the table loudly — it orphans every recorded objection while `may_contact` goes
on answering "allowed" for the people who said no. Re-keying would mean re-hashing from identifiers
we deliberately do not keep, so it cannot be done: the key is generated once, kept off the machine
as well, and a rotation requires a migration plan that starts by collecting the identifiers again.
An **empty** key refuses to record and refuses to check, rather than hashing with a key anybody can
recompute. It reaches the `web` container through `infra/compose.yaml` and is pinned by
`marceline/tests/test_compose_env.py`, because a setting that file does not pass in is silently
empty for ever.

### It lives in `catalog`, and it is not index suppression

`catalog` already holds `Creator` (prospects are `Creator` rows with a `purpose=prospect` watchlist
entry) and `CreatorSuppression`, so the optional link costs no new dependency: the contract is
`catalog → core`, and nothing here needs more. The project package would have been the alternative —
it may import anything — but the record has no customer surface, no tenancy question and no API, and
putting a `Creator` foreign key there would mean a model in `marceline/` pointing at `catalog/` for
no gain. A new app would buy isolation we do not need for one table. The pure rules sit in
`catalog/do_not_contact.py` and the two Django-side operations in `catalog/services.py`, the same
split as `catalog/hosting/`.

**ADR 0038 answers a different sentence.** Index suppression is "stop indexing me"; this is "stop
writing to me". Neither implies the other — someone may want to be left alone by post and stay in
the index, or the reverse, or both — so neither one applies the other, and each staff screen says
the other exists. `suppress_creator` deletes the prospect trail and does **not** touch these rows: a
prospect who became a customer and left is still someone who said no.

### Permanent

There is no retention job and no expiry: the record remains for as long as it is needed to prevent
the objection from being forgotten. Deleting the record would re-enable the contact it exists
to prevent. Concretely: no delete permission in the admin, no cascade from `Creator` (`SET_NULL`),
`suppress_creator` leaves it alone, and the customer-data deletion of **ADR 0045** (issue #85) does
not reach it — no module of `marceline/deletion/` imports `catalog` at all, and two tests in
`marceline/tests/test_customer_data_deletion.py` pin it: an organisation pass that deletes the
organisation, its user and its originals leaves the record and its creator standing, and no module
of the package may grow a `catalog` import.

## Consequences

- Every outbound contact now starts with `may_contact`.
- The record can only recognise identifiers it was given. It is not a global "this person" marker
  and must never be described as one, to the operator or to a recipient.
- Nothing about a recorded objection can be read back, including by us. A staff member who wants to
  know whether a particular address is on the list asks `may_contact`; there is no browsing.
- The key becomes a piece of state as durable as the database, and as unrecoverable as a lost
  encryption key. `.env.example`, the settings comment and `docs/do-not-contact.md` all say so.
- If the operator ever needs more than "blocked/allowed" — a report of how many objections arrived,
  per channel — the counts are already in the table; the identifiers are not, and no future feature
  may add them.
