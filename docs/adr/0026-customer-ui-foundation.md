# 0026 — Customer UI foundation: bilingual from the start, its own shell, its own sign-in

Status: accepted (2026-09-16)

Implements phase 1 of `docs/customer-ui-plan.md` (issue #32) in the browser: the i18n seam, the
customer app shell and sign-in against the contract of ADR 0023. **The data screens are not part of
this change** — the overview, original detail and case file land on the seam this ADR describes.

## Context

ADR 0021 built the customer API and ADR 0023 the customer login; both said the UI was deferred.
Deferring it any longer had a cost the operator named explicitly: the product is bilingual, and
**retrofitting translation is far more expensive than starting with it**. A German market with
international creators means every screen exists twice from the first commit, or it exists once in
German and gets rewritten later.

The second decision that shapes this: the customer UI is a *separate audience*, not a widened staff
screen. The staff SPA is a queue for someone who decides. This is an answer for someone whose work
is being stolen.

## Decision

### i18next + react-i18next, with resources as typed modules

Two dependencies (`i18next`, `react-i18next`), and nothing else. It is the smallest thing that does
the job properly: interpolation, real plural rules via `Intl.PluralRules` (German and English both
need one/other, and a wait of "1 Minute" versus "15 Minuten" is exactly the kind of string a
hand-rolled `Record<string, string>` gets wrong), a language-change event React re-renders on, and
a provider so the staff SPA is untouched.

What was deliberately *not* added:

- **`i18next-browser-languagedetector`.** Detection is fifteen lines and a policy decision — stored
  choice, then the browser's languages, then English — so it lives in `src/i18n/language.ts` as
  pure functions with tests rather than in a dependency whose defaults we would have to read.
  English is the fallback for a browser asking for neither language, because showing German to an
  international creator who cannot read it is the worse failure.
- **A component library or any theming.** Theming is deferred by the operator; this uses the
  existing Tailwind conventions (ADR 0004, ADR 0014) and the existing `components/ui.tsx`
  primitives.
- **Resource JSON fetched at runtime.** Resources are TypeScript modules, bundled: no request, no
  loading state, no Suspense boundary, and they are typechecked.

`en.ts` is the shape of the resource set. `de.ts` is annotated with `Translation = typeof en`, so a
key added to one and forgotten in the other is a **typecheck** error, before any test runs.

### A half-translated UI fails the build

`src/i18n/i18n.test.ts` flattens every language's resources and asserts, against English as the
reference:

1. **the same keys**, in both directions — a missing translation and a stray leftover both fail,
   and the failure names the keys;
2. **no empty strings** — an empty string is a missing translation that looks present;
3. **the same interpolation placeholders per key** — a dropped `{{count}}` renders a sentence with
   a hole in it, which the key comparison alone would not catch;
4. **complete plural sets** — a `_one` without its `_other`;
5. **every `t("…")` key in `src/app/` exists in the resources** — a typo otherwise renders the key
   itself to a paying customer, and TypeScript cannot see inside a string.

Language is chosen from the browser, switchable explicitly (the switch is visible *before* sign-in,
since someone who cannot read the form cannot sign in to change it), and the choice is persisted in
`localStorage` under `marceline.language`. Unreadable storage is not an error; the browser's
languages decide again next time. `document.documentElement.lang` follows the choice.

### The customer app is mounted apart from the staff routes

`/app/…`, with its own layout. The router now has a pass-through root with two branches: a pathless
layout route holding every staff screen at its existing path and its existing staff-only gate, and
`/app` with the customer shell. **No staff URL changed**, and a customer never renders staff
navigation — not because a flag hides it, but because it is in the other branch of the tree.

Inside `/app`, the session gate is itself a pathless route (`app-protected`). A new customer screen
becomes a child of it and is behind the gate by construction, rather than by remembering to check.
An unauthenticated visit to any `/app` route lands on `/app/sign-in`.

### Sign-in says exactly what ADR 0023 answers, and no more

`GET /api/v1/app/auth/session` first — it is the only thing that sets the `csrftoken` cookie, and
the login POST is refused without it — then `POST /api/v1/app/auth/login` with the token echoed as
`X-CSRFToken` by the client middleware the SPA already had. The mapping from response to message is
pure (`src/app/signInError.ts`), so each branch is pinned by a test:

| Answer | What the customer is told |
|---|---|
| **200** | nothing; they are signed in and land on the placeholder |
| **401** | one message for unknown account, wrong password and disabled account alike. The backend is deliberately indistinguishable across the three; **saying which it was would undo that** and turn the form into an account-enumeration oracle |
| **403**, no organisation | the password was right, the account has no customer organisation, and a staff reviewer is pointed at the internal interface. Safe to say, because it follows a correct password |
| **403**, CSRF | "the form expired, try again" — a stale token is not the account's fault, and blaming the account would be a lie |
| **429** | the wait, from `Retry-After` (seconds or HTTP-date), in whole minutes or seconds. **Shown, never acted on**: no timer, no automatic second attempt |

The login mutation is `retry: false`. A 429 must not become a retry loop, and a wrong password must
not be sent twice — five failures lock an account for fifteen minutes.

There is no sign-up link and no "forgot password" link, because neither endpoint exists (#50). The
form says so instead of linking to a 404.

### No imagery, enforced rather than promised

Issue #53 is open, so the customer UI shows no copy of the material — and, more
strongly, **contains nothing able to load a remote image**. Two independent enforcements:

- **eslint**, for `src/app/**`: no `img`/`video`/`picture`/`source`/`iframe`/`canvas`/… elements, no
  `src`/`srcSet`/`poster` attribute, no `backgroundImage`, no absolute `href`, no importing the
  staff media components, and no reference to `mediaImageUrl`;
- **`src/app/privacy.test.ts`**, which scans the customer source text for the same constructs plus
  the ones a linter cannot see (a CSS `url()` in a class string, `/api/v1/media/` in a template
  literal). It exists so that relaxing the eslint config does not quietly relax the ban.

There is therefore no image component at all. If a screen ever legitimately needs one, it takes an
id for our own passthrough and is incapable of expressing any other URL — and the test is amended
to say exactly that, which is the conversation it should trigger.

### The seam for the data screens

Three marked places, and nothing else to undo:

1. `src/router.tsx` — the data screens are added as children of `appProtectedRoute`;
2. `src/app/HomeScreen.tsx` — a marked section the overview replaces;
3. the rules that hold for whatever lands there: strings from `src/i18n` in both languages, and no
   imagery.

The placeholder reads `GET /api/v1/app/me` and shows the organisation name, segment and role. That
is the whole point of it: it proves session, CSRF, tenancy scope and language end to end, and it
shows nothing a customer has not already been told.

## Consequences

- **`frontend/package.json` and `package-lock.json` changed** (two runtime dependencies). Anyone
  merging alongside this runs `make frontend-install`.
- `ApiError` now carries `retryAfterSeconds`, parsed from the response in `unwrap`. Additive: the
  staff SPA behaves as before, and the staff login could use it for a friendlier 429 (ADR 0023 left
  that as UI work).
- The router gained a pass-through root and a pathless staff layout route. Staff paths, gate and
  screens are unchanged; the staff SPA has no i18n and no customer code in its tree.
- The customer UI is **responsive web only** and carries no theming. Both are non-goals in the plan.
- The tab title comes from the resources, so it is no longer "Marceline review" for a customer.
- `index.html` still declares `lang="en"`; the customer app sets `documentElement.lang` at runtime.
  Server-rendered HTML is not in play, so there is nothing else to translate there.

## Alternatives rejected

- **German first, English later.** The operator's decision, and the reason this ADR exists: the
  retrofit costs more than the seam, and the seam is cheap on day one.
- **A hand-rolled `Record<string, string>` lookup.** Fine until the first plural and the first
  interpolation, then it becomes a worse i18next with no tests behind it.
- **FormatJS / react-intl.** ICU messages are more powerful than two languages of UI copy need, and
  it is a larger dependency with a build step for the message catalogue.
- **A second Vite entry point (a separate customer SPA).** Two builds, two deploy targets and two
  copies of the API client, to separate two route branches that share a session mechanism. The
  route-branch split gives the same guarantee — a customer cannot render staff navigation — for the
  cost of one pathless route.
- **Gating sign-in inside the layout, as the staff SPA does.** It works, but it leaves no URL to
  link a customer to, and "you are signed out" then has no address. `/app/sign-in` is a real route.
- **Showing a countdown for the 429.** A live timer is a loop of a different kind, and the wait is
  the backend's fact, not ours to animate.
- **Reusing the staff `Layout`/nav with the staff items filtered out.** One forgotten condition and
  a customer sees the safety queue. The separate shell makes it structurally impossible.
