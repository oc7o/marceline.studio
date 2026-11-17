# 0023 — Customer login: one user model, throttled, no signup

Status: accepted (2026-09-16)

Closes the gap ADR 0021 left open ("there is no customer login endpoint yet") and answers open
question 3 of `docs/customer-ui-plan.md`. Backend and API only; the customer UI is still deferred.

## Context

`/api/v1/auth/login` refuses any account without `is_staff` by design, so nothing built in ADR 0021
could actually be used by the audience it was built for: a customer session could only be created
through Django's own admin login. The operator asked for sign-in, and asked one question with it —
**one Django user model for staff and customers, or separate account types?**

## Decision

### One user model, separated by authorisation, not by table

A customer is a `auth.User` with an `accounts.Membership`. A staff reviewer is a `auth.User` with
`is_staff`. The same account can be both, and one test asserts exactly that.

The recommendation was checked against the code rather than accepted on principle, and it holds:

- `accounts.Membership.user` is already a FK to `settings.AUTH_USER_MODEL`, and
  `marceline/app/ownership.py` resolves every customer queryset through it. A second identity table
  would need a second ownership path, which is the one module ADR 0021 went out of its way to keep
  singular;
- the review API's authorisation is `is_staff` (`marceline/review/auth.py`) and the customer API's
  is membership (`marceline/app/auth.py` + the 404 rule). Neither reads the other's marker, so the
  boundary is already independent of which table the row sits in;
- `marceline/tests/test_app_tenancy.py` fixes the behaviour that matters — staff get 404s and an
  empty list from `/app/…`, and a member of another organisation is indistinguishable from a
  missing row. Those tests pass unchanged after this change, which is the evidence that the
  separation never depended on table separation;
- `seed_test_customer` and the admin already create customers as ordinary users.

Two identity tables would mean two login paths, two password-hash configurations, two session
mechanisms and two lockout implementations — four places to get security wrong in exchange for a
boundary that is already enforced twice (membership, and 404-not-403). The things that *would*
argue for separate tables — mandatory MFA for staff, different password policies, different session
lifetimes, a regulatory requirement to hold customer identity separately — are all reachable on one
table with a per-user or per-group policy, and none of them is a requirement today. If one ever
becomes one, the change is a policy check at login, not a migration.

What this decision does **not** mean: a customer is not "a user who happens not to be staff".
Ownership is membership in an organisation; an account with neither marker is a real account that
owns nothing and may see nothing (ADR 0021), and that remains a 200 with an empty list, never a
403.

### Endpoints

`/api/v1/app/auth/…` (`marceline/app/session.py`), beside the customer router, session-based:

| Endpoint | Answers |
|---|---|
| `GET /api/v1/app/auth/session` | is this browser signed in, and here is the CSRF cookie |
| `POST /api/v1/app/auth/login` | start a customer session |
| `POST /api/v1/app/auth/logout` | end it |
| `GET /api/v1/app/me` (existing) | the account and the organisation it is scoped to |

`login` answers **200** with the same body as `/app/me` (one shape for "who am I and what am I
scoped to"), **401** for any credential failure, **403** when the password was right but the
account belongs to no organisation, and **429** when a limit has been reached. The 403 is said only
*after* a correct password, so it tells a stranger nothing they did not already supply; a staff
reviewer signing in at the customer door is told to use theirs.

Session handling is the staff mechanism unchanged (ADR 0014): Django's session cookie, `login()`
rotating the session key and the CSRF token, and the SPA reading the `csrftoken` cookie and echoing
it as `X-CSRFToken`. No token scheme was invented, and none should be: a same-origin SPA behind
Caddy has no problem that a bearer token in JavaScript-reachable storage would solve.

Cookies are now configured explicitly rather than inherited: `SESSION_COOKIE_SECURE` and
`CSRF_COOKIE_SECURE` default to on whenever `DJANGO_ENV=prod`, the session cookie is `HttpOnly`,
both are `SameSite=Lax`, and `DJANGO_CSRF_TRUSTED_ORIGINS` carries the HTTPS origins. The CSRF
cookie stays readable by JavaScript because that is how the SPA uses it; it is not a credential.

### Rate limiting and lockout

A login endpoint on the public internet needs limits that survive several gunicorn workers and a
deploy, so the counter is a Postgres table (`accounts.LoginAttempt`), not process memory (what
`review/media.py` uses for image passthrough would give an attacker one budget per worker) and not
Redis (ADR 0002: no new infrastructure until a table hurts). All of it lives in
`accounts/login_throttle.py` and is shared by the staff login, which was on the same public
internet all along.

| Limit | Value | Setting |
|---|---|---|
| Failures per account before refusal | **5** | `LOGIN_MAX_ACCOUNT_ATTEMPTS` |
| Failures per IP before refusal | **20** | `LOGIN_MAX_IP_ATTEMPTS` |
| Window failures are counted in | **15 minutes** | `LOGIN_ATTEMPT_WINDOW_MINUTES` |
| Refusal, measured from the last counted failure | **15 minutes** | `LOGIN_LOCKOUT_MINUTES` |
| Reverse proxies in front of Django | **0** | `LOGIN_TRUSTED_PROXIES` |

Choices inside that, each deliberate:

- **the account bucket is keyed by the submitted username**, not by a user row. An account that does
  not exist locks out exactly like one that does, so the lockout can never answer "is this a real
  account". A test asserts the two 429s are identical;
- **the check runs before the password is verified.** A refused attempt costs no hash, is not
  counted again and does not extend the lockout, so a locked-out customer always recovers fifteen
  minutes after their last counted failure. The accepted cost of any per-account lockout is that
  someone who knows a username can lock its owner out for that window; the window is short for that
  reason, and `manage.py unlock_login --user NAME` lifts it at once;
- **a correct password clears the account bucket but not the IP bucket.** Otherwise one account an
  attacker controls would reset their spray budget at will;
- **rows identify nobody.** `key` is an HMAC-SHA256 of the scope and value under `SECRET_KEY`;
  counting needs equality and nothing else, and a plaintext table would be a log of who tried to
  sign in from where (issue #49). Consequence: rotating `SECRET_KEY` clears every lockout, and the
  admin can see that something is locked but not what;
- **`X-Forwarded-For` is ignored by default.** Believing it turns the per-IP limit into a header an
  attacker rewrites per request. Behind Caddy, set `LOGIN_TRUSTED_PROXIES=1` and the address is
  taken one entry from the right — the part a client cannot forge. Left at 0 behind a proxy the
  per-IP bucket becomes global, which is why it is written down here and in `.env.example`.

### A failed login says exactly one thing

Unknown account, wrong password and disabled account produce the same 401 and the same body, and
each of them pays for a password hash: Django's `ModelBackend` hashes the submitted password even
when the username is unknown, and checks the password before it checks `is_active`. The test asserts
both halves — identical responses, and a hash computed on every path — because the second is what
makes the first true in time as well as in content.

### No self-service signup

Accounts are created by the operator (admin, or `manage.py`), and there is no registration endpoint
anywhere in the API. Onboarding and KYC are not decided yet (#50), and a product whose subject
matter is other people's private material cannot let strangers create accounts before that question
is answered. The absence is asserted (`test_the_api_offers_no_way_to_create_or_reset_an_account`)
and stated in the endpoint's own OpenAPI description, so adding signup means deleting a test that
names the reason — which is the conversation it should trigger.

### Password reset is deferred, deliberately

The operator resets passwords by hand: `manage.py changepassword <username>`, over the channel the
account was set up on. Django's reset flow is not wired up.

Why not now, given the mailer exists: the flow needs a verified email address per account (accounts
are created by hand today and the username happens to be an address, which is not the same thing),
a customer-facing page for the link to land on (there is no customer UI yet — it is deferred), its
own throttling, and a decision about how much an unauthenticated reset endpoint may reveal. Each is
answerable; none is answerable *well* before onboarding (#50) says how a customer's identity is
established in the first place. Half a reset flow — a mail that goes nowhere, or a token page that
does not exist — is worse than a phone call to the operator while there is one customer.

Revisit when either is true: the customer UI ships, or the account count passes the handful the
operator can serve personally. The implementation is then Django's built-in
`PasswordResetView`/`PasswordResetConfirmView` wired to `MAILERS` (`crawler/alerts.py` shows the
transport), behind the same `login_throttle` buckets — not a hand-rolled token scheme.

## Consequences

- **A migration ships with this change** (`accounts/0002_loginattempt`). Apply it from the main
  checkout with **`make migrate`, and restart the workers** — a stale container writing against the
  old schema is how 997 pages were lost on 2026-09-15.
- **The staff login is throttled too** and can now answer 429 with `Retry-After`. That is a new
  status on `/api/v1/auth/login`, so `backend/openapi.json` changed and
  `frontend/src/api/schema.d.ts` needs `make frontend-api` from the main checkout (a worktree has no
  `node_modules`). The moderation SPA treats it as an error today; a friendlier message is UI work.
- An operator locked out by their own testing waits fifteen minutes or runs
  `manage.py unlock_login --user NAME`.
- `accounts.LoginAttempt` grows only with failures and is purged past the window plus the lockout on
  every write, so it needs no cron job.
- Session lifetime is now stated rather than inherited: fourteen days, which is what Django's
  default already was, but as `SESSION_COOKIE_AGE_SECONDS` it can be shortened without a code
  change when the product has real customers to argue about it with.

## Alternatives rejected

- **A separate customer account model (or a swapped `AUTH_USER_MODEL` per audience).** Two identity
  tables, two login paths, two password flows, two session mechanisms — and the tenancy boundary it
  would claim to enforce is already enforced by membership and the 404 rule. Rejected on the
  operator's question above.
- **A token scheme (JWT or similar).** Nothing in a same-origin SPA needs it, and it would replace a
  battle-tested cookie with storage the page's own JavaScript can read. The brief said not to invent
  one; there was no reason to want to.
- **django-axes or another dependency for lockout.** Roughly a hundred lines here, no new dependency
  in a lock file two other agents are working in, and the rules (digest keys, refuse before hashing,
  submitted-username bucket) are exactly the ones this product needs.
- **Counting in the Django cache.** The default cache is per-process memory: several gunicorn
  workers, several budgets, and a deploy resets them all.
- **A 403 for an account with no organisation, said before checking the password.** It would turn
  the endpoint into an oracle for "is this account a customer". The check happens after.
- **Implementing password reset now** — see above. **Self-service signup** — blocked on #50.
- **Email magic links instead of passwords.** A plausible future for this audience, but it needs the
  same verified-address groundwork as reset, and it replaces the login the SPA already speaks.
