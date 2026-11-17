# 0054 — Address verification and password reset, by mail, from development only

Status: accepted (2026-09-19)

Issue #149. Operator decision, 2026-09-19: the private beta needs a password reset and an address
verification, mailed from `noreply@marceline.studio`. Extends ADR 0023 ("no password reset" — this is
the decision that lifts that one, deliberately and with a mechanism), ADR 0028 (the write path) and
ADR 0052 §9 (whose road this takes).

## Context

Until today the operator reset a password with `manage.py changepassword` and handed it over on a
second channel. That was honest at one account and is not a beta: a creator who cannot sign in on a
Sunday has no way back in, and the sign-in screen said so ("there is no self-service reset"), which
is a sentence that costs a customer.

Two things make it harder here than in any Django tutorial:

- **the public host holds no authority over an account.** It replicates accounts from development
  through a signed document (ADR 0028) and never writes customer data into its own tables. A reset
  changes `User.password`, which is development's to change;
- **the public host has no mail transport, and must not get one.** It is the machine on the internet.
  A credential-bearing link is minted and sent by the machine that holds the accounts, the
  fingerprints and the keys — and that machine is not reachable from the internet at all.

Address verification comes with it, for a narrow reason: every account's address was typed by staff
into an invite (ADR 0052). Nothing has ever confirmed that the address reaches the person, and the
first mail we send is the proof.

## Decision

### 1. Development mails; production only asks and accepts

Four writes, the same road as a redemption (ADR 0052 §9, option b):

| What | On development | On the public host |
|---|---|---|
| "send me a reset link" | finds the account, issues a token, sends the mail | `OutboxItem` `password_reset_request`, 202 |
| "here is my new password" | sets it, ends every session | hashes it, `password_reset_set` with the **hash**, 202 |
| "this address is mine" | records it | `email_verify`, 202 |
| "send that mail again" | sends it | `email_verify_resend`, 202 |

`accounts.AccountActionToken` is the row: a kind (`password_reset`, `email_verification`), the user,
the **SHA-256 of the token**, an expiry, and the same `handed_over_at` / `submission_id` pair that
makes an invite single-use on a host that cannot see `redeemed_at` yet. The token itself exists in
exactly one place: the mail. `accounts/action_tokens.py` holds the shape, the lifetimes and the one
refusal; it reuses the invite's `new_token` / `token_hash` rather than growing a second pair.

**The password never travels in clear.** Production runs `validate_password` (the error has to reach
the person typing) and `make_password`, and only the hash is in the payload; `apply_password_reset`
sets `User.password` from it as it arrives and refuses a string `identify_hasher` does not recognise,
because that string becomes a credential. `PASSWORD_HASHERS` is the project's default on both hosts
and is not to be diverged — the same rule, for the same reason, as ADR 0052 §9.

**Production can refuse a link by itself.** The signed accounts document gains an `action_tokens`
section: per open token the digest, the kind, the account's id and the expiry. No token, no address —
production reads the address off its own replicated user row, which is what lets it run Django's
`UserAttributeSimilarityValidator` against the same account development would. The section is
**absent** while the flag is off, exactly like `invites`: `AccountsPayload` forbids extra fields, so a
production on an older image would refuse a whole document and stop learning about accounts at all.

**No transport on the public host, ever.** `infra/prod/check_config.py` refuses `EMAIL_*` or
`ACCOUNT_MAIL_FROM` in production's environment, so the obvious way to "make the mail work there"
fails the deploy gate instead of working.

### 2. The request tells nobody whether an address has an account

`POST /api/v1/app/auth/password-reset` answers **202 `mail_requested`** for an address with an
account, without one, belonging to a staff reviewer, inactive, and when the transport refused. There
is no branch anywhere that formats another sentence, and the screen's words are conditional in
themselves: "if this address has an account, a mail is on its way."

Timing, precisely:

- **on the public host it is constant**, because production does not look the address up *at all*.
  It cannot: its replica may be fifteen minutes old, so it is not authoritative for whether an
  account exists. Every request is one enqueue;
- **on development the mail is sent inline**, so a known address costs a transport round trip that an
  unknown one does not. That surface is Tailscale-only (ADR 0028) and is not a place a stranger can
  measure. Written down rather than hidden: if development's customer surface ever becomes public,
  this has to move to a queue first.

Throttling is ADR 0023's buckets, reused rather than rebuilt: the account bucket is keyed on the
submitted address for a request and on the submitted token's **digest** for a redemption, the IP
bucket is the shared one. A 429 tells a caller only about their own attempts. Nothing new stores an
address or a token in the clear.

A link that does not work answers **one 404 with one body** (`link_not_available`): unknown,
malformed, wrong kind, expired, already used, handed to the outbox, flag off, no transport. The rule
and the reasoning are ADR 0052 §6's, applied to a second doorway.

### 3. A reset ends every session; verification blocks nothing

**Sessions.** Django already invalidates a session when the password changes
(`get_session_auth_hash`), but only on the host that holds the session — and production learns the
new password with the next `pull_accounts`, up to fifteen minutes later. So production deletes that
account's sessions **itself**, in the request that accepts the reset: the session table is
production's own data (ADR 0028), so this is a local write and not a customer write. Development does
the same when it applies. `accounts/sessions.py` is the one implementation.

**Verification state** is `accounts.EmailVerification.verified_at`, a row rather than a column
because `AUTH_USER_MODEL` is `auth.User` and this project does not add a second identity table
(ADR 0023). The **moment** replicates to production, not a flag, so both hosts hold the same fact.

**An unverified address blocks nothing in the beta.** The question the brief asked, decided and
justified: staff issued the invite *to* that address (ADR 0052 §1), so it is already as good as the
operator's own record of it — refusing a signed-in customer their own screens over an unopened mail
would invent a wall where there is no doubt, and the first thing it would break is the case of
somebody whose mail client ate our mail. What it does change is what we may **rely on** later: a
notice's reply-to, a deletion confirmation, a warning that a pilot is ending. Those read
`verified_at` when they exist; until then the app renders one quiet line in the shell with a link,
and `GET /app/me` reports `email_verified` and `can_verify_email` as facts.

A redeemed invite asks for the confirmation at once, from `transaction.on_commit` — a transport
taking seconds must not hold the invite's row lock, and a redemption must not fail because a mail
server is down.

### 4. The mail is two short texts, and they are the whole surface

`accounts/mail_templates.py`, verbatim, DE („du", operative — the agreement is German) and EN:

- **plain text, no HTML part** (`EmailMessage`, not `EmailMultiAlternatives`, so the type is the
  guarantee). Nothing to render, nothing to load, nothing that can carry a tracking pixel;
- **exactly one link**, on the customer app's own host. No unsubscribe (this is not marketing), no
  help centre, no logo, no web font, no redirect through a click tracker. A second link would train
  creators that our mails carry several, which is what the phishing aimed at them looks like;
- `Auto-Submitted: auto-generated` (RFC 3834) and a `Reply-To` that reaches a human, because
  `noreply@` must not be a dead end for somebody who did not ask for the mail;
- **no name, no organisation and no address in the body**: it arrived in the reader's own inbox, and
  telling them their own address back only travels with a forward;
- the reset mail says what to do **if it was not them**, and the verification mail says that nothing
  is blocked by it. Both name their lifetime in words, from the setting.

The link's origin is `ONBOARDING_INVITE_BASE_URL` — deliberately reused rather than joined by a
second setting for the same value, because two variables naming one origin are two chances to send a
creator to a host that does not serve them. Its name is historical.

### 5. One flag, and no transport means no flow

`ACCOUNT_EMAIL_FLOWS_ENABLED`, default **false**. Off: every route answers its neutral response and
nothing is sent, nothing is issued, nothing is queued, and the document carries neither section. On
development the flag alone is not enough — `action_tokens.available()` is the flag **and** a
configured mailer there, because a token nobody can receive is worse than a refusal. On production it
**is** enough: that machine never mails (see the amendment of 2026-09-20, issue #153). Both halves have to reach the two processes that send —
`web` (a request on development's own surface, and a redeemed invite) and `export`
(`pull_customer_writes`, which applies what the public host accepted); `test_compose_env.py` checks
it, because a variable `infra/compose.yaml` does not pass in is silently empty for ever.

### 6. Four screens, three of them outside the session gate

`/app/forgot-password`, `/app/reset-password/{token}`, `/app/verify-email/{token}` — ungated by
definition: somebody who can sign in does not need the first, and the point of the second is that
they cannot. `/app/verify-email` (the resend) is inside the gate. `lib/routes.test.ts` names all five
ungated pages and fails on a sixth: outside the gate is a decision, never a default.

Two details worth the words:

- **a GET confirms nothing.** Mail clients, scanners and link previews fetch links on their own, so
  the verification page shows a button and the POST is the act. An address confirmed by a scanner is
  a confirmation nobody made;
- **nobody is signed in by a reset.** The link proves the reader has the inbox, not that they know
  the password they just chose, and a session minted from a mailed link is a session a forwarded mail
  could have. Both endings — set (development) and accepted (the public host) — point at the sign-in
  form, and the second says so honestly rather than claiming the password is already live.

## Consequences

- **The operator has to provision a mailbox**, and it is the gate on the flag: a sending address for
  `noreply@marceline.studio`, an SMTP host, and a reply address that a human reads
  (`contact@marceline.studio`). The env keys by name: `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`,
  `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `ACCOUNT_EMAIL_FLOWS_ENABLED`,
  `ACCOUNT_MAIL_FROM`, `ACCOUNT_MAIL_REPLY_TO`, and `ONBOARDING_INVITE_BASE_URL` pointing at the
  public app.
- **DNS**: SPF (the provider's `include:`), DKIM (the provider's keys) and a DMARC record for
  `marceline.studio`. Without them a reset mail lands in spam, which is indistinguishable, to the
  creator and to us, from a reset that does not work.
- **The privacy notice must name the mail provider as a processor** (Art. 28 GDPR) and say that an
  address is used to send these two mails, before `ONBOARDING_DOCUMENTS` can be filled.
- **Deploy order**, the same as ADR 0052 slice 2 and for the same reason: merge to `main`
  (development learns the four outbox kinds) → **the promotion** (production must know the document's
  new fields before development emits them) → the flags, plus `OUTBOX_ENABLED=true` on production.
- Two migrations: `accounts/0010` (the two models) and `delivery/0008` (the kinds). The token table
  cleans itself up: `purge_expired` runs when a token is issued, which is rare.
- `docs/runbooks/production.md` gains the section that switches it on, and the sign-in screen loses
  the sentence that said there was no reset.

## Amendment 2026-09-20 — the transport is development's requirement, not the flow's (issue #153)

A release-readiness review found the feature broken on the host it was built for. `available()` was
`flows_enabled() and mail_configured()` and every lookup asked it first — but the public host is
**refused** a mail transport by `infra/prod/check_config.py`, and its default backend is SMTP with no
host. So with the switch on there, `available()` was false for ever: every mailed link answered the
neutral 404, the resend answered 404, and development cheerfully mailed links the public app then
refused. Exactly the scenario §1 exists to prevent, and measured with a production-shaped
environment, not reasoned about.

**The predicate is per role**, because the two machines do different halves of one flow:

- **development**: the flag **and** a transport. It mints and mails; without a mailer it would mint
  links nobody can receive;
- **production**: the flag **alone**. It never mails — it takes the request or the redemption into the
  outbox and development does the rest — so asking it for a capability it is designed not to have was
  the bug, not the safeguard.

Whether the **outbox** can take a write stays a separate question with its own answer: the write paths
answer 503 (`account_action_unavailable`, `processing_paused`), which is temporary and must not be
confused with a link that will never work.

`available()` no longer implies "may mail here", so two things say that instead:
`action_tokens.mails_here()` (the flag, a transport, **and** not the prod role) is what
`request_password_reset` and `request_verification` ask, and `accounts/mail.py::_send` refuses by role
at the one place a mail can leave the process. The role check used to be implied by the absence of a
transport; now it is written down.

**The request route asks the flag before it writes anything.** It went straight to `outbox.enqueue`,
so an anonymous caller could put rows into production's queue while the feature was off. It answers
the **same** neutral 202 with the flag off — §2 owns that sentence, and "off" must not be a second,
distinguishable answer — but nothing is queued, minted or sent.

**Why no test caught it**, which is the part worth keeping: the production test played production with
`MARCELINE_ROLE=prod` in an environment where the test runner had installed the **locmem** backend, so
its production had a transport the real one is refused. `test_account_flows_on_production.py` now
pins production by its *settings* — `PROD_MAILERS` is the SMTP backend with no host, which is what
`infra/prod/compose.yaml` yields — and one helper (`as_development`) is the only place the role and the
mailer change, together. Reverting `available()` to the old expression fails 13 of that file's 16
tests, including the full round trip and a guard that makes `mail_configured()` **raise** on
production, so a code path that reaches for it fails a test instead of quietly refusing a creator.

**The invite path does not have this shape of mistake** (checked, issue #153 asked): `invites_enabled()`
is the flag, `published_documents()` reads a setting both hosts carry, and the hand-over asks
`outbox.writes_enabled()` — a 503, not a neutral 404. Nor do the others: `adding_originals_available`
and the rights/notices `writes_available` are `role != prod or outbox.writes_enabled()`,
`uploads_available` asks for the relay on production deliberately (ADR 0044 §9), `export_available` is
development-only by design and its route is dev-only, and ADR 0055's `available()` is its flag alone.
Mine was the only predicate that asked production for a capability it may never have.

## Alternatives rejected

- **Django's own `PasswordResetView` and `default_token_generator`.** Its token is a signed HMAC over
  the user's password hash and `last_login`, which is elegant and wrong here: it is verifiable by
  anybody holding `SECRET_KEY`, which means the public host could mint one, and it needs no row —
  so there is nothing to replicate, nothing to expire on our own clock, and no way to make a link
  single-use across two hosts. The row is the point.
- **Production sending the mail itself.** It would need SMTP credentials on the machine on the
  internet, and a compromised production could then mail a link it minted to an address it chose.
  The whole read-through cache exists to keep that machine out of the authority business.
- **A magic sign-in link instead of a password reset.** It turns the mail into a permanent
  credential and makes the inbox the account. ADR 0052 rejected it for invites; nothing changed.
- **Verification as a gate.** Considered and refused in §3: staff issued the invite to the address,
  and a wall there punishes the wrong person.
- **A second setting for the link's origin.** Two variables naming one origin are two chances to
  send a creator somewhere that does not serve them.
