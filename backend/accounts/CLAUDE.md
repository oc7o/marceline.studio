# Accounts: users, organisations, and who we are allowed to sign

The root `CLAUDE.md` hard rules and `backend/CLAUDE.md` apply here too. `accounts` may import
`core` and nothing else — no `catalog`, no `marceline`.

## Tenancy (ADR 0021, ADR 0023)
- `Organisation` is what every customer request is scoped to; `Membership` is a user's one place in
  one of them (a database constraint, not a convention). `is_sandbox` marks the demo organisation
  (ADR 0035) and nothing else.
- **A `Membership` is the grant of customer access.** `GET /api/v1/app/me` reports
  `can_add_originals` exactly when a membership exists and this machine takes writes
  (`marceline/app/account.py`). There is no self-service signup anywhere: accounts are created in
  the admin or with `manage.py`, by decision (issue #50).

## Business status: the *Unternehmer* evidence (issue #92)
- **A tick-box converts nobody.** The pilot's B2B-only, EU-only scope (operator decision,
  issue #95 item 7) rests on the evidence recorded at signature, not on the declaration alone.
- `BusinessStatus` is that record, one-to-one with the organisation: the kind of evidence from a
  closed list, a note saying what was checked, the VAT ID, the country, who looked and when, and
  the customer's own declaration with its **verbatim wording and version**.
- **No identity documents**, the same rule as `RightsClaimant` (ADR 0034): staff record *that* they
  saw a document and enough to find it again, never its contents, never a copy, and no document
  number about a person. The VAT ID has its own field on purpose — it identifies an undertaking,
  is published for verification, and giving it a field keeps numbers out of the free-text note.
- **The model records; `business_status.py` judges.** A half-filled row saves; what is missing
  comes back as codes (`CODES`, one order, one definition that the model, the guard and the admin
  filter all read). `clean()` only refuses what would make a *stored* row misleading.
- **A country outside the EU is flagged, never refused** (`OUTSIDE_EU` is not in `MISSING_CODES`):
  a Swiss extract is complete evidence of a party outside the pilot's scope, and scope is a
  commercial decision.
- **The guard sits on `Membership.clean()`**, because that is the step that grants access.
  `ONBOARDING_BUSINESS_STATUS_GUARD` decides: `warn` (the default — the admin shows a message and
  saves) or `refuse` (the membership form rejects it). Anything else reads as `warn`; a typo must
  not refuse every membership. The seeding commands use `create`/`get_or_create`, which never call
  `full_clean`, and `seed_test_customer` passes `enforce=False` on top of that, so the test
  customer and the demo dataset keep working whatever the setting says.
- **A new declaration wording is a new version in `declarations.py`, never an edit** (the rule of
  `marceline/rights/declarations.py`). The German text is the operative one; the English is a
  reading aid. Stored rows carry a text snapshot, so an old version must stay in the file.
- **The majority declaration is the second field group on the same row** (issue #123, ADR 0052
  amendment 2026-09-19): `age_declaration_version` / `age_declaration_text` / `age_declared_at`,
  all-three-or-none in the database like the *Unternehmer* one, and `age_declaration_missing` is one
  of `CODES`. It is a **declaration, not a verification**: no date of birth is asked for or stored
  (Art. 5(1)(c) GDPR) and no identity document is accepted — never add a birth-date field "to be
  able to check". The wording is a **draft**: a correction is a new version.
- Nothing here is customer-facing: no API field, no screen.

## Login throttling (ADR 0023)
`LoginAttempt` rows identify nobody — `key` is an HMAC of the scope and value under `SECRET_KEY`.
Only `login_throttle.py` writes that table; lifting a lockout is `manage.py unlock_login`.

## Onboarding by invite (issue #115, ADR 0052) — off by default
- **It is not signup.** ADR 0023 stands: no stranger can make an account. `accounts.CustomerInvite`
  is a link *staff issue* for a person they have already written to; what changes is who does the
  clerical part of onboarding.
- `accounts/onboarding.py` is pure (token shape, documents, steps, the one refusal),
  `accounts/invites.py` writes, `marceline/app/invites.py` answers HTTP. **`lookup()` is the only
  way in** and returns `None` for unknown, malformed, expired, revoked, redeemed and
  "feature off" alike — **never add a caller that asks which**, that is the enumeration hole.
  `REFUSED` (`invite_not_available`) is the one thing any layer may say.
- **The token is never stored**, only its SHA-256; it is printed once at issue and is not
  recoverable. Throttling is ADR 0023's buckets, keyed on the token's digest and the IP — nothing
  new stores a token or an address in the clear.
- **The declaration step signs two wordings** (issue #123): § 14 BGB and majority/legal capacity,
  one screen, one moment, **one tick box each** and one refusal code each. `step_of` counts a
  declaration as signed only while it names the **current** version, so a redemption left across a
  wording change signs today's text; the write carries the versions the screen displayed and answers
  `declaration_version_outdated` otherwise. Those payload fields have defaults on purpose — a
  required field would answer 422 *before* the token check, and the one neutral 404 comes first.
- **Nothing is created until the last step.** The password lives on the invite as a Django hash,
  the declarations verbatim; `complete()` is one transaction (organisation, user, membership with
  `full_clean` so the issue #92 guard applies, `BusinessStatus`, the acceptances' owner,
  `redeemed_at`) under `select_for_update`, so a double submission is one redemption. **The step is
  derived (`step_of`), never stored.**
- **`ONBOARDING_DOCUMENTS` empty refuses the accept step**, and empty is the default: the pilot
  agreement and the service privacy notice are drafts until published, so the flow cannot go live ahead
  of them by accident. `accounts.DocumentAcceptance` is **append-only** and stores the document's
  id, version and the **sha256 of the published text**, never the paragraphs.
- **A country outside the EU is counted, never stored, and does not consume the invite** (ADR 0052
  §5): a typo on a select box must not cost somebody their invitation.
- **The evidence stays a staff step.** A redeemed account is *not* `business_status_evidenced`; the
  screen says the account is being looked at. `ONBOARDING_BUSINESS_STATUS_GUARD=refuse` makes the
  last step refuse rather than grant access.
- **It runs on the public host too since 2026-09-19** (ADR 0052 §9, slice 2, amendment of that
  date): the prefix has left `ROLE_ONLY_PREFIXES`, because the creator holding the link is not on
  the tailnet. Production still creates **nothing**. `complete()` refuses outright while
  `MARCELINE_ROLE=prod`; the last step calls `lock_for_handover` → `redemption_of` →
  `delivery.outbox.enqueue` → `mark_handed_over` in one transaction and answers 202, and development
  applies it with `apply_redemption`, which ends in that same `complete()`. **Only the hash of the
  password travels** (production makes it with `make_password`, `apply_redemption` refuses a string
  `identify_hasher` does not know), never the token, never the address — the payload names the
  invite by its digest.
- **`handed_over_at` is what makes single use hold on production**, before development has seen
  anything: `lookup()` stops finding the invite, so every step including a second `complete` gets
  the one neutral 404 and no second outbox item can exist. It belongs in `lookup()` and **not** in
  `usable()` — development's apply path reads the row directly and must not be stopped by it.
- **Production's invite rows are replicated, never authored there**
  (`marceline/accounts_replica.py`): the signed accounts document carries each open invite's token
  **digest**, expiry, address and destination, and a pull writes only those fields — a creator
  halfway through keeps their password hash, declarations and acceptances. An invite that left the
  document is deleted, which is how a revocation reaches that host (up to one pull cycle late: a
  revocation is an operational control, not a security one).

## The two mailed account actions (issue #149, ADR 0054) — off by default
- **Development mails; the public host only asks and accepts.** A reset or a verification requested
  there becomes a `delivery.OutboxItem`; development issues the token, sends the mail and applies the
  write. Production holds **no mail transport and never will** (`make prod-config` refuses one): a
  credential-bearing link is minted by the machine that holds the accounts.
- `accounts/action_tokens.py` is pure (the token's shape — the invite's own `new_token`/`token_hash`,
  reused — the two lifetimes, the one refusal `link_not_available`, and `available()`), `mail_templates`
  holds the two texts, `mail` hands one to a transport and **never raises**, `account_actions` writes,
  `sessions` ends sessions, `marceline/app/account_actions.py` answers HTTP.
- **`available()` is per role, and that is the point** (issue #153, ADR 0054 amendment 2026-09-20).
  On **development** it is the flag *and* a configured mailer: `ACCOUNT_EMAIL_FLOWS_ENABLED` alone
  would mint tokens nobody can receive. On **production** it is the flag **alone** — that host never
  mails and is refused a transport by `make prod-config`, so asking it for one made every mailed link
  answer the neutral 404 there with the switch on. **Never re-add `mail_configured()` to the
  production side**; a test makes it raise on that role. Whether the outbox can take the write is a
  separate, temporary question and answers 503, never the neutral 404.
- **`mails_here()` is "may this process send"**: the flag, a transport **and** not the prod role. It
  is what `request_password_reset` / `request_verification` ask, and `mail.py::_send` refuses by role
  at the one place a mail can leave the process.
- **`lookup()` is the only way in** and returns `None` for unknown, malformed, **wrong kind**, expired,
  used, handed to the outbox, flag off and no transport alike — never add a caller that asks which.
  A verification link presented as a reset link is refused like an unknown one.
- **A request says nothing about the address.** `request_password_reset` returns nothing a caller
  could branch on, and only a **member of an organisation** is ever mailed: a staff account's password
  stays the operator's business (ADR 0023), and a mailed link into one would be a way in that ADR 0028
  keeps off the internet.
- **The token names the user, not the address**: a reset sets the password of its own row's user, so
  an address that changed in between cannot move a credential. Only the **hash** of a new password
  travels (`checked_password_hash` refuses a string Django does not recognise — it becomes
  `User.password`).
- **A reset ends every session, on both hosts** (`accounts/sessions.py`): Django's session auth hash
  does it for the host that changes the password, and production deletes the account's sessions itself
  because it learns the new password up to fifteen minutes later.
- **`EmailVerification.verified_at` blocks nothing** (ADR 0054 §3): staff issued the invite *to* that
  address. It is a fact `/app/me` reports and a later feature may rely on — never a gate. The moment
  replicates, not a flag.
- **The mails are the whole surface**: plain text, one link on our own host, `Auto-Submitted`, a
  `Reply-To` that reaches a human, no tracking, no address in the body, „du" in German. A wording
  change is a change in `mail_templates.py` with its test (`accounts/tests/test_account_mail.py`).
