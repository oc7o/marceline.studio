# 0056 — The creator asks for an export or a deletion from inside the app

Status: accepted, 2026-09-19. Behind `CUSTOMER_DATA_REQUESTS_ENABLED`, off by default. Closes the
in-app half of issue #133. Builds on ADR 0045 (the
deletion machinery and the export document), ADR 0028 and ADR 0046 (what reaches the public host:
reads from signed bundles, writes through the outbox), ADR 0021 (customer API and tenancy).

## Context

The machinery exists. `manage.py export_customer_data` renders every document a customer's account
holds (ADR 0045), `GET /api/v1/app/export` serves the same document to the customer themselves, and
`manage.py end_pilot` starts the retention clock that deletes an organisation's data
`CUSTOMER_DATA_RETENTION_DAYS` later. None of it was reachable by the person it is for.

The beta-readiness audit of the invited-creator journey (#133, 2026-09-19) found why:

- `GET /api/v1/app/export` is development-only (`public_surface.ROLE_ONLY_PREFIXES["dev"]`), because
  production holds no originals, no rights records and no notice drafts of its own — only what a
  signed bundle carries. So `me.can_export_data` is false there and the app hides the link;
- there was **no deletion endpoint at all**, on either host. The only writers were staff commands;
- an invited creator reaches the public host and nothing else. Development is on Tailscale.

So a pilot creator could exercise neither Art. 15/20 GDPR nor Art. 17 from inside the product, and
this is the one gap in that journey with a **statutory deadline** attached: Art. 12(3) gives one
month to answer, counted from the request, whether or not anybody noticed it.

Point 1 of #133 — naming the channel in writing — was answered in the documents (the service
privacy notice's C1 and the pilot agreement's § 16 Abs. 7 name `contact@marceline.studio` and the
one-month deadline). That is honest and it is not enough: a creator who has just been told in a
15-page document that they may write to an address has no way to tell, later, whether they did, when,
and whether anybody is working on it — and neither have we.

## Decision

### 1. A request is a request. It exports nothing and deletes nothing.

`marceline.deletion.CustomerDataRequest` records that a creator asked, of which kind, when, and
where staff stand with it. It is not a trigger:

- **an export is still produced by a person** — `manage.py export_customer_data` plus a delivery the
  operator makes, out of band for now (§5);
- **a deletion clock is still a date a human sets** — `manage.py end_pilot`, unchanged, with its
  confirmation tick and its 30-day window. ADR 0045's central rule ("nothing infers a date") is not
  being bent: a customer-triggered request is the *least* verified input in the product, arriving
  over a session cookie on a public host, and the one operation that cannot be undone does not start
  from it. ADR 0045 anticipated this case in so many words — "if a customer-triggered *close my
  account* arrives, it sets the same `ended_at` and the window still applies" — and this is that
  arrival: a human reads the request and sets the date.

What the creator gets is the thing that was missing: a recorded, dated, followable request, with the
statutory deadline and the address stated next to it.

### 2. Two kinds, one open request each

`export` (Art. 15, Art. 20) and `deletion` (Art. 17). A partial unique index
(`data_request_one_open_per_kind`) allows at most one open request per organisation per kind, so a
second press of the button is not a second request — the API answers 409 `already_open` and the
screen reads it as "we have it" rather than as an error. A settled request is never rewritten into a
new one: it stays as the record of what was asked and when.

The paths name the **kind**, not a request id: `POST /api/v1/app/data-requests/{export,deletion}`
and `.../withdraw`. With one open request per kind there is nothing ambiguous about "withdraw my
open deletion request", and there is no id a caller could point at another organisation's row with.
Tenancy is therefore structural rather than checked: every read and write starts at
`organisation_of(request.user)`, another organisation's request is invisible, and a withdraw finds
nothing and says `not_open` — byte for byte what a caller with nothing open gets.

### 3. A deletion request needs a typed word, checked on both machines

The creator types the word the server sent (`deletion_phrase`, `DELETE`) before the request is
taken. It is **not translated**: the document a public host serves comes out of a signed bundle and
is language-independent (ADR 0028), so the check has to be language-independent too.

It is verified twice — by the host that takes the request, and again by development when the write
arrives through the outbox. A write that travels between two machines is re-judged on the machine
that acts on it, and a deletion request is the last write in this product to take on trust.

### 4. It reaches the public host the way rights and notices do

Not the way the export download does. The read is one bundle document per organisation
(`data-requests`, in `export.LIST_DOCUMENTS`, `store.DOCUMENT_KEY` and
`customer_cache.CACHED_ROUTES`), rendered by calling the customer view as one of the
organisation's own members, so production serves exactly the bytes development would have. The three
writes are two outbox kinds (`data_request_open`, `data_request_withdraw`) answered **202** on
production and applied by development with the same functions the development endpoints call
(`marceline/customer_writes.py`). There is no `ROLE_ONLY_PREFIXES` entry.

The switch has to be set on **three** processes, and a guard test says so
(`marceline/tests/test_compose_env.py`): `web` (the routes and the admin screen), the public host,
and development's `export` service — which is where `manage.py pull_customer_writes` runs. That last
one is the quiet failure this feature could have: without it a creator's request would be accepted
on production, pulled, and refused `data_requests_unavailable`, with the creator told it arrived and
no staff member ever seeing it.

### 5. Staff answer it, and the daily report makes sure they notice

*Marceline → Customer data requests* in the admin shows each request with its date, the computed
Art. 12(3) due date, and `status` + `staff_note` as the only editable fields. There is no add
button: a request is made by the creator, in the app.

Answering is deliberately **not** a button here. For an export it is `export_customer_data` and a
delivery the operator makes; for a deletion it is `end_pilot`, i.e. the screen above, which is the
one place a deletion clock starts. They are separate acts by a person, and a request that could
start either would be exactly the shortcut §1 refuses.

The daily operator report (`crawler/safety_digest.py`) gained one line — `Data requests waiting: N
(oldest: D days)`, with the admin link and the statutory month named — and the count reaches the
subject line whenever there is one. It is the only deadline in that mail a statute sets. The report
lives in `crawler`, which may not import `marceline` (import-linter), so the digest holds a
two-integer value type and a hook that `marceline/apps.py` fills at startup; a machine that could
not count says "not counted" rather than "0".

### 6. The screen

`/app/data` in the Next customer app: a synchronous server component with one island for the three
writes. It shows each request's standing, its date, the due date, the one-month deadline and the
contact address — the address comes from the API (`contact_email`), so no UI string spells it. An
open request offers "take it back", not a second "ask again" the server would refuse. It is linked
from the shell beside the export download and sign-out, **not** as a sixth navigation place: the
phone's bottom bar has five slots of 72 px (issue #117).

The development-only direct download stays exactly where it is. On a machine that offers both, a
creator can download their data *and* ask for a copy to be sent; on the public host only the latter
exists, which is the whole point.

## Consequences

- An invited creator on the public host can exercise Art. 15/20 and Art. 17 from inside the product,
  and both the creator and we can see the request afterwards. Answering stays manual and dated.
- The privacy notice needs one sentence added: that the app itself now offers both requests, beside
  the address that already answers them. Until it does, the documents are *more* cautious than the
  product, which is the safe direction.
- Two things this deliberately does **not** do: serve the export document from the public host
  (that would need the bundle to carry every original's stored hashes — real work, its own ADR
  amendment, and #133 point 3 left open), and let a customer's press start a deletion.
- `CUSTOMER_DATA_DELETION_ENABLED` is untouched and still off: it guards the only code that deletes
  anything. A request recorded while deletion is switched off is still a request somebody answers.

## Alternatives considered

- **A mail link only** (what the documents describe today). It is the fallback and it stays in the
  footer, but it leaves no record either side can point at, and the deadline then depends on
  somebody noticing a mail.
- **A button that sets `ended_at` directly.** Rejected: see §1. It would put the product's one
  irreversible operation behind a session cookie on the public host.
- **Serving `GET /app/export` from the bundle.** Rejected for now (#133 point 3): production holds
  no originals or rights records, so the bundle would have to carry the whole export document per
  organisation, and the export is the heaviest document in the product.
- **A request id in the path.** Rejected: with one open request per kind it buys nothing and adds an
  id a caller could aim at another organisation's row.
