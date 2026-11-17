# 0052 — Creator onboarding by invite

Status: accepted (2026-09-18)

Issue #115. Amends ADR 0023 ("no self-service signup") without lifting it, and extends ADR 0028's
write path to a write that happens **before** an account exists.

## Context

The product's primary audience is now the independent creator. A
creator who says yes today gets an account through twelve manual steps in the Django admin, over
Tailscale, by one person, for every account.

That is the bottleneck, and most of it is clerical: an organisation row, a user row, a password set
with `manage.py changepassword` and handed over on a second channel, a membership, a declaration
typed into a form, an acceptance recorded in a note. The parts that are *not* clerical — the
§ 14 BGB evidence (issue #92), the rights claimant check (ADR 0034) — are judgement and stay with a
human.

What must not change is the decision underneath it: **no self-service signup** (operator,
2026-09-16; ADR 0023; `docs/customer-ui-plan.md` §8 answer 3). A product whose subject matter is
other people's private material does not let strangers make accounts.

## Decision

### 1. An invite is the account, before it has a password

Staff issue an invite; the creator redeems it. There is still no registration endpoint a stranger
can reach with a self-chosen address: every redemption path takes a token staff generated, and a
request without one gets the same 404 as an unknown path.

`accounts.CustomerInvite` is the row. It carries the e-mail address the account will have, the
organisation it leads into (an existing one, or a name to create), who issued it, an optional note,
an expiry, and the progress of a half-finished redemption. **The token itself is never stored** —
only `token_hash`, a SHA-256 of the random 32-byte token, the same discipline as
`delivery.ServiceToken`. The token is printed once, at issue, and cannot be recovered afterwards.

Single use: `redeemed_at` is set in the transaction that creates the account, and the row's state
machine has no way back. Revocable: `revoked_at`, set by staff, with a reason that is never shown
to anybody outside the admin.

### 2. The operator sends the link himself

The project sends no customer mail today. So v1 **prints the link** — `manage.py invite_customer` and the
admin action both show it once — and the operator pastes it into the message he is writing anyway.

The seam for mailing later is `accounts/invites.py::deliver`, which today only returns the link and
records `delivered_by="operator"`. `ONBOARDING_INVITE_MAIL_ENABLED` does not exist yet on purpose:
a flag that switches on an unwritten transport is worse than no flag.

### 3. The accept step refuses until real documents are published

The pilot agreement and the service privacy notice are **not published yet**. A
flow that presents a draft as the thing someone agrees to is worse than no flow.

So the documents the redemption presents come from a setting, `ONBOARDING_DOCUMENTS`, which names
for each document its id, its **published** version, the sha256 of the exact text that is served,
and where the text lives. **Empty is the default**, and empty means the accept step refuses and no
invite can be completed. The feature therefore cannot go live ahead of the documents by accident —
which is the whole reason the version list is a setting and not a constant in code.

What is stored per acceptance (`accounts.DocumentAcceptance`, append-only, no update path): the
document id, its version, the **sha256 of the text that was presented**, the language it was
presented in, and the moment. Not the text: the text is the published artefact and is addressed by
its digest, so a stored digest that no longer matches any published file is itself the finding.

### 4. The business declaration is the creator's half; the evidence stays staff's

The redemption shows the versioned `unternehmer-…` wording from `accounts/declarations.py` — the
German text operative, the English a reading aid, exactly as that module says — and stores it
verbatim with its version and timestamp into `accounts.BusinessStatus`.

It does **not** record evidence. `BusinessStatus.evidence_kind`, `evidence_note`, `checked_by` and
`checked_at` stay empty until a human looks, and `business_status_evidenced` is therefore false for
a freshly redeemed account. The guard of issue #92 already says what that means
(`ONBOARDING_BUSINESS_STATUS_GUARD`: `warn` or `refuse`), and the redemption honours it: with
`refuse` the last step cannot complete and the creator is told the account is being set up by hand.

The creator is told the truth either way: the account exists, it is **being checked**, and what
they can already do (add their originals) versus what waits for the check. A tick-box alone
establishes nothing, and a flow that pretended otherwise would be the
one place in the product that lies about its own state.

### 5. EU-only: a non-EU answer ends politely, and the invite survives

The pilot is EU-only (operator, issue #95 item 7). The country step asks where the business is
established. A country outside the EU ends the redemption with a plain sentence and no account.

**The invite is not consumed.** A typo on a select box must not cost a creator their invitation and
the operator a round trip; the invite still expires on its own clock, and a guesser gains nothing,
because reaching this step already required the token. What is recorded is a **count**
(`outside_eu_answers`), never which country was answered: we have no account and no contract with
this person, so the only fact worth keeping is that the scope was wrong for somebody, as many times
as it happened. An invite whose count is high is an operator conversation, not a data point.

### 6. One neutral page, for every reason a token does not work

Unknown, expired, already redeemed, revoked, and "the feature is off" produce **one identical
answer**: the same status, the same body, the same page. There is no branch anywhere that formats a
different sentence, and a test asserts the responses are byte-identical.

The reason is enumeration: an invite is addressed to a person, and an answer that distinguished
"never existed" from "expired" would tell whoever holds the link something about whether an address
was invited. Timing is the same by construction — the lookup is one indexed query on the token
hash, and no branch does more work than another before answering.

Throttling is ADR 0023's, reused rather than rebuilt: `accounts/login_throttle.py`, the same
Postgres buckets, the same HMAC'd keys. The account bucket is keyed on the **submitted token's
hash** (so guessing one token is limited wherever it comes from) and the IP bucket is the shared
one (so spraying tokens from one host is limited across the product). Nothing new stores an address
or a token in the clear.

Every write is CSRF-checked exactly as `app/session.py` checks the login, and every answer carries
`Cache-Control: private, no-store` through `CustomerNoStoreMiddleware`, which already covers
`/api/v1/app/…`.

### 7. The route names are a prefix, so a prefix can gate them

All of it lives under **`/api/v1/app/invite/`**. One prefix, so `public_surface.ROLE_ONLY_PREFIXES`
can hold it on development while the production path is unbuilt, and one line removes it when it is
built. It is under `/api/v1/app/` because it is a customer surface — "public by construction" is
the rule of ADR 0028, and this is the one customer endpoint that is deliberately reachable without
a session.

`GET /api/v1/app/invite/{token}` answers the state; `POST /api/v1/app/invite/{token}/{step}`
advances it. State lives on the invite row, so every step is resumable: a creator who closes the
tab and opens the link again lands on the step they were on.

### 8. The account is created in one transaction, at the last step

Nothing is created until the last step: no user, no organisation, no membership. A password set at
step one is held on the invite as a **Django password hash** (`make_password`, the project's
configured hasher) and moved onto the user row at completion; a declaration is held on the invite
and copied into `BusinessStatus`. An abandoned invite therefore leaves no account behind, and
`accounts.CustomerInvite` is the only row to clean up.

The completing transaction: create or fetch the organisation, create the user with the held hash,
create the membership (`full_clean`, so the issue #92 guard applies), create `BusinessStatus`,
attach the `DocumentAcceptance` rows to the user, stamp `redeemed_at`. `select_for_update` on the
invite row makes a double submission the same single use.

### 9. Production: the redemption is an outbox item (option b)

This is the hard part, and the constraint is ADR 0028: the public production host holds accounts
**only** through `pull_accounts`, never writes customer data into its own tables, and cannot call
development (the Tailscale link goes the other way — development pulls).

Three options were on the table.

**(a) Redemption only on development's customer surface.** Correct in one line of code and useless:
that surface is Tailscale-only. A creator cannot reach it. Rejected.

**(c) Production calls development to redeem.** It would reverse the one-way trust of ADR 0028 and
give the public host a credential that creates accounts on the machine holding every original's
fingerprints. Rejected outright — this is the direction the whole cache design exists to avoid.

**(b) The redemption is an outbox item.** Taken. Production validates what it can, stores the write
as `delivery.OutboxItem`, answers 202, and development applies it on its next pull (up to one
minute), creating the user, the organisation, the membership, the `BusinessStatus` and the
acceptances through `marceline/customer_writes.py` — the **same functions** the development
endpoint runs, never a second implementation. `pull_accounts` brings the new account back to
production within its 15-minute cycle, and only then can the creator sign in.

What that costs and how each cost is paid:

- **Production must be able to refuse a bad token without an account database.** So the signed
  accounts document gains an `invites` section: per open invite the **token hash**, the expiry, a
  hash of the target e-mail address, and the organisation's public id or a placeholder. No address,
  no name, no note. Production checks the hash and the expiry itself and answers the same neutral
  page for anything else. An invite revoked on development disappears from the next document, so
  the worst case is one 15-minute window in which a revoked token is still accepted — the write is
  then refused on application and the creator is told the invitation is no longer valid. That is
  the same class of staleness the cache already has, and it is why revocation is not a security
  control but an operational one.
- **The password must never travel in clear.** Production hashes it with Django's configured
  hasher before the item is enqueued — the same `make_password`, the same PBKDF2 parameters (the
  project does not change `PASSWORD_HASHERS`), so `User.password` is set from the hash as-is on
  development, and development never sees the password. Validation (`validate_password`) runs on
  production too, because the error has to reach the person typing.
- **Replay.** An outbox item is applied once (`InboxItem` written in the same transaction as the
  apply, ADR 0028). Replaying a batch re-applies nothing. Replaying the *HTTP* redemption is
  handled by the invite: a token whose item is pending is no longer offered the last step, and
  development's apply is itself guarded by `select_for_update` on the invite plus `redeemed_at`.
- **Double redemption across the 15-minute window.** Two browsers can both reach step "complete"
  on production, because production does not hold `redeemed_at`. Both enqueue; both are applied in
  order per organisation; the first creates the account, the second finds `redeemed_at` set and is
  settled as **applied with no effect** — not quarantined, because it is not an error, it is the
  same person pressing twice. The second browser sees the same "your account is being set up".
- **Quarantine after 3 failures.** Unchanged from ADR 0028: `MAX_APPLY_ATTEMPTS`, the exception
  *class* as the reason, never its message (a redemption payload holds a password hash and an
  address). A quarantined redemption is an operator alarm, and the creator sees the account never
  appearing — which is why the staff view of open invites is part of this feature and why
  `make status` counts them.
- **What the creator sees.** Accepted: "your account is being set up, try signing in in a few
  minutes" — a factual line, never "done", the same rule the notice drafts follow. Refused before
  acceptance (bad token, expired, off): the one neutral page. Refused after acceptance (revoked in
  the stale window, address taken): nothing automatic, because production has no channel to tell
  them; they find sign-in refusing them, and the operator finds the item in the admin. That is the
  honest weakness of the design and the reason the pilot's invites are issued one at a time by a
  person who then watches for the account.
- **Ordering at deploy: development first.** An unknown outbox kind quarantines (ADR 0028), so the
  handler must exist before production can enqueue the kind. `invite_redeem` therefore ships to
  development, is deployed, and only then does production's build learn to enqueue it.

### 10. It ships in two slices, and slice 1 is off the production surface

Slice 1 — this ADR's code — is the whole flow on the customer surface of **development**: the model,
the staff tooling, the endpoints, the Next screens, the tests. `/api/v1/app/invite` sits in
`public_surface.ROLE_ONLY_PREFIXES["dev"]`, so production answers the same bare 404 it answers for
`/api/v1/app/uploads`. This is testable end to end today and is how the operator's first invites go
out while he is on Tailscale himself, or while the pilot runs on development.

Slice 2 is §9: the outbox kind, the accounts document's `invites` section, production's local
refusal, and the removal of that one prefix line. It is a separate branch because it touches the
signed document format and the write path, and because the deploy has an order. **It was built on
2026-09-19** — see the amendment below, which decides four things this section left open and
differs from §9 in one.

### 11. Everything behind one flag

`ONBOARDING_INVITES_ENABLED`, default **false**. Off: every `/api/v1/app/invite/…` route answers
the same neutral 404, and the admin action and `manage.py invite_customer` refuse with the setting's
name. An invite that already exists is not honoured while the flag is off — the flag is the switch,
not a filter.

## Consequences

- Two migrations: `accounts/0006` (`CustomerInvite`, `DocumentAcceptance`) and `accounts/0007`
  (its indexes). Apply with `make migrate` from the main checkout and restart the workers.
- A new customer surface that is reachable **without a session**. It is the only one, and it is why
  §6 exists. `marceline/tests/test_cross_origin.py` and `test_public_surface.py` walk it like any
  other route.
- The manual onboarding loses its clerical steps to code and keeps the
  judgement: the § 14 BGB evidence, the rights claimant, `needs_review` creators, and granting
  `can_add_originals` where that is still a decision.
- The redemption cannot be completed until `ONBOARDING_DOCUMENTS` names published versions, which
  keeps the feature behind the documents rather than in front of them.
- `make status` gains one line: invites open / redeemed / expired.

## Amendment 2026-09-19 — the declaration step carries a second declaration (issue #123)

The declaration step signs **two** wordings, not one: the `unternehmer-…` declaration of §4 and a
new `volljaehrigkeit-…` declaration of majority and full legal capacity, both from
`accounts/declarations.py`, both stored verbatim with their version and the moment, both moved onto
`accounts.BusinessStatus` at completion. The screen's name changes with it ("Your statements" /
„Ihre Erklärungen"); the flow is still four steps.

**Why it is one step and not a fifth.** Two sentences on one screen, agreed to in one moment, are
one decision for the person reading them; a redemption that had signed one and not the other would
be a state `step_of` could not describe without a second cursor. Each sentence keeps its **own**
tick box and its own refusal code (`declaration_required`, `age_declaration_required`), because a
single box over two sentences is not agreement to either.

**Why the declaration exists at all — and what it is not.** No statute requires an age check of a
customer of this service. The reason is narrower: a contract with a minor is voidable (§§ 107 ff.
BGB) and every case file we would then build rests on it. So this is a **declaration, not a
verification**: no date of birth is asked for or stored
(Art. 5(1)(c) GDPR), no identity document is accepted, and nothing is inferred from any other field.
A correction of the wording is a new version in `declarations.py`, never an edit, which is why this must
ship before the first invite goes out rather than after.

**A superseded version does not count.** `step_of` treats a declaration as signed only while it
names the current version, so a redemption left half-finished across a wording change comes back to
the step and signs today's text. The write carries the version the screen displayed and answers
`declaration_version_outdated` when it is no longer current — the same rule as the rights
declarations (`marceline/rights/services.py`). Every field of that payload has a default although
none is optional: a required field would be rejected by the schema *before* the view looked at the
token, and §6's one neutral 404 has to come first.

**What staff see.** `BusinessStatus.age_declared_at` is a column on the admin's list, the invite's
three fields are read-only on its detail, and `age_declaration_missing` joins the codes of issue
#92, so an organisation whose holder never declared it is not `business_status_evidenced`.

## Amendment 2026-09-19 — slice 2 is built: the redemption works on the public host (issue #115)

Operator decision, 2026-09-19: invite links must work on `dash.marceline.studio` for the private
beta. §9 is implemented as written, option (b); `/api/v1/app/invite` has left
`public_surface.ROLE_ONLY_PREFIXES`. Four things are decided more narrowly than §9 left them, and
one differs from it.

**The credential.** `POST /api/v1/app/invite/{token}/password` runs on production exactly as it runs
on development: `validate_password` (so the error reaches the person typing) and `make_password`. The
plain password never leaves the process that received it, the hash is held on production's invite row
and only the hash travels in the outbox item; `apply_redemption` sets `User.password` from it as it
arrives and refuses a string `identify_hasher` does not recognise, because that string becomes a
credential. `PASSWORD_HASHERS` is the project's default on both hosts and is not to be diverged.
`mark_handed_over` clears the hash from production's row in the same write that stamps the hand-over:
production has no use for it again.

**The `invites` section carries the address, not a hash of it.** §9 said "a hash of the target e-mail
address". Two reasons overrode that. Django's `UserAttributeSimilarityValidator` compares the
password with the username, and the username *is* the address — with only a digest the two hosts
would refuse different passwords, and the redemption would fail on development after the person had
already been told their password was fine. And the screen names the address the account will have,
which is how somebody knows which invitation they are redeeming. It is also not a new class of data
on that host: the accounts document already carries every customer's address as their username, and
this carries it for at most the invite's lifetime and only while the invite is open. Everything else
§9 named stays out — no note, no issuer, no declaration, and above all no token: the section is keyed
by the token's SHA-256, so a leaked document hands nobody a working link.

**Single use across the two hosts, and what the second browser sees.** `handed_over_at` on the invite
row is production's own marker, written in the transaction that enqueues the item, under
`select_for_update`. From then on `lookup()` does not find the invite, so every step *including*
`complete` answers the one neutral 404 and no second item can be enqueued. This differs from §9,
which expected the second browser to see "your account is being set up": that would be a second
answer on the surface whose whole design is one answer, and it would distinguish a token used a minute
ago from one that never existed. The browser that finished holds its own panel; a second browser sees
what it sees for an invite already redeemed on development. `complete()` additionally refuses
outright while `MARCELINE_ROLE=prod`, so the public host cannot create an account by a later caller's
mistake rather than only by the view's good behaviour.

**What production judges for itself.** The token's shape and digest against the replicated open
invites, the expiry, the revocation (as absence from the document), the flag, Django's password
validators, that the documents it displayed are the ones `ONBOARDING_DOCUMENTS` publishes — the same
setting, which must name the same versions and digests on both hosts — and the step order. Everything
else is development's: the declarations' current wording, the invite's true state, `Membership.
full_clean()` and the issue #92 guard. A redemption refused on apply is a code on the outbox item in
the admin (`invite_not_available`, `declaration_version_outdated`, `documents_outdated`,
`account_exists`, `business_status_not_evidenced`) and the invite stays open, so the operator can
finish it by hand; the creator is told nothing, because there is no channel to somebody who has no
account yet. That is §9's honest weakness, unchanged.

**The deploy has an order, and it is not the usual one.** §9 says development first, because an
unknown outbox kind quarantines — and merging to `main` gives development the handler, so that half
is automatic. The *document* pulls the other way: `AccountsPayload` forbids extra fields, so a
production still running the previous image would refuse a whole accounts document that carried
`invites` — and with it every account and password change. The `invites` key is therefore absent
while `ONBOARDING_INVITES_ENABLED` is off on development, and the order is: **merge to main** (the
handler exists on development) → **promote to production** (`docs/runbooks/release.md`) → switch the
flag on, on development and on production. Production also needs `OUTBOX_ENABLED=true`: without it
the last step answers 503, because there is nowhere to put the redemption.

**Still gated by the documents.** `ONBOARDING_DOCUMENTS` is empty by default on both hosts and empty
refuses the accept step, so no invite can be completed anywhere until the pilot agreement and the
service privacy notice are published — which remains the point of ADR 0052 §3, and is why this
slice's deployment is not the same event as the first invitation.

## Amendment 2026-09-20 — a deletion clock for the invite that is never redeemed

A review of the first release found the gap: `CustomerInvite` holds personal data before any
account exists — the invited address, a staff note, the two declarations' verbatim text, the
answered country and, until redemption, a Django password hash — and an invite that is never
redeemed (expired, revoked or simply abandoned) had no deletion clock. A **redeemed** invite was
never the problem: it becomes an account in the same transaction that stamps `redeemed_at`, and
from then on the row is audit trail for a real customer, cascading away with the organisation like
any other.

**The fix is a clock, in the house pattern of `page_retention` / `research_retention` /
`customer_data_retention`.** `accounts.invite_retention.apply_retention` (management command
`invite_retention`, dry run by default) deletes an unredeemed invite
`ONBOARDING_INVITE_RETENTION_DAYS` (default 30, matching `CUSTOMER_DATA_RETENTION_DAYS`) after it
stopped being usable — the revocation timestamp for a revoked one, `expires_at` for one that simply
ran out; a still-open invite is never a candidate, whatever the period. No new field: both
timestamps already existed. Scheduled daily, guarded by `INVITE_RETENTION_ENABLED` (off by default,
same class of switch as every other irreversible-deletion job), inserted into `marceline.scheduler
.JOBS` **before** the fixed `rights_housekeeping … research_retention` tail rather than after it —
that tail's exact order is pinned by position in two existing tests, and accounts data has no
ordering dependency on any of it, so this avoids touching their assertions' logic while still
running daily.

**"Never redeemed" is `redeemed_at IS NULL`, not `organisation IS NULL`.** The review's own
phrasing named `organisation IS NULL` as part of the "never redeemed" description; that is wrong for
an invite that joins an *existing* organisation, whose `organisation` field is set from the moment
`invites.issue` creates the row — it names the destination, not the outcome, and stays set whether
or not the invite is ever redeemed. Only `redeemed_at` (and `redeemed_user`, written in the same
statement by `invites.complete`) is set exclusively by redemption, so that is the field the module
filters on. Using `organisation IS NULL` instead would have left every abandoned "join an existing
organisation" invite's personal data with no working clock — the opposite of what the review asked
for.

**The handed-over case.** An invite whose finished redemption is sitting in the outbox
(`handed_over_at`, §9) is excluded outright and reported separately (`in_flight`), never silently
skipped. On development's own authoritative row this field is in practice always empty — a
handover only ever happens against production's *local* replicated copy of the row
(`marceline/accounts_replica.py`), and development's own `complete()` never sets it — but the
filter does not rely on that: it excludes `handed_over_at IS NOT NULL` regardless of which host
runs the command.

**What this changes on production: nothing.** `marceline/export.py::_open_invites` already excludes
a revoked or expired invite from the signed accounts document, and
`marceline/accounts_replica.py::_apply_invites` already deletes production's local copy of an
invite that has left that document on the very next pull. By the time a row is a candidate for
`invite_retention` it has already dropped out of what production reads; deleting it on development
changes nothing about what production holds. Production runs no scheduler and needs no equivalent
job of its own.

**`accounts.DocumentAcceptance.invite` is `on_delete=SET_NULL`**, unchanged by this amendment: an
accepted document's record survives the invite row's deletion.

**Known gap, not closed by this amendment.** ADR 0057's deletion journal (`core.deletion_journal`)
is meant to cover every irreversible privacy deletion in the codebase, and `invite_retention` is
not wired into it — no `"invite_deleted"` kind exists in `core.deletion_journal.KIND_FIELDS`, and
no handler exists in `marceline.deletion_replay`. A restore from before an `invite_retention` run
would therefore silently undo it with nothing to replay. Left for a follow-up change, because
registering a new journal kind touches `core/` and `marceline/deletion_replay.py`, both outside
this change's file ownership.

## Alternatives rejected

- **Opening signup with e-mail verification.** It is the same decision ADR 0023 already took, for
  the same reason, and nothing about the audience change makes strangers safer to admit.
- **A magic link instead of a password.** It needs a mail transport this project does not have, and
  it would make the invite link a permanent credential rather than a one-time one.
- **Creating the user at the password step and finishing later.** Half-built accounts on the
  identity table, a password that exists before any document was accepted, and a cleanup job. The
  invite row is a better place to hold a half-finished conversation.
- **Storing the accepted document text instead of its digest.** It would put the same paragraphs in
  every row and invite editing them. The digest plus the published artefact answers the same
  question and fails loudly when the artefact moves.
- **Telling the creator which of expired/used/revoked applies.** It is an oracle about an address,
  and the operator can see the reason in the admin, where the question actually gets answered.
