# 0036 — Phase 2: the customer app on Next.js with a backend-for-frontend

Status: accepted (2026-09-17)

Builds phase 2 of ADR 0028 section (d): the customer app (`/app`) moves from the Vite SPA to a
Next.js server that renders the pages and is the only thing a customer's browser talks to. The
staff moderation SPA does not move — it stays Vite, stays on development, stays Tailscale-only.

## Context

ADR 0028 accepted Next.js as the phase-2 customer frontend with a performance budget, and made
load time an acceptance criterion: **first load ≤ 130 KB gzip**, ≤ 30 KB gzip per additional
route. Phase 1 (ADR 0028 d.1, issue #82) split the Vite SPA by route and stopped shipping the
inactive language, which brought `/app`'s first load from 188 KB to 145 KB gzip — and then hit a
wall that is structural, not a missed optimisation: a fully client-rendered app pays for React, a
client router and a query cache on every visit (~102 KB gzip measured before one line of ours).
Phase 1's budget check therefore guards 150 KB, not 130 KB, and says so in its own comments — and
two features later (notice drafts, the catalogue overview) the SPA's heaviest routes are **at that
ceiling**: 149–151 KB per fresh visit, with nothing left to give.

Phase 2 removes that floor's largest part by rendering on the server: the browser gets HTML, and
only the interactive islands ship JavaScript.

## The measurement that decides the framework version

Before writing any of it, a hello-world App Router page (one server component, one client
component, `next build`, production) was measured the way a browser pays for it: the gzip size of
the `<script>` elements the page loads. Next emits its legacy polyfill chunk with `noModule`, so a
modern browser skips it; it is excluded (38.45 KB gzip, and it would be paid by IE-era browsers
only).

| Next.js | bundler | first load, gzip | our room under 130 KB |
|---|---|---|---|
| 15.5.25 (`backport` dist-tag, last published 2026-08-31) | webpack | **100.1 KB** | ~30 KB |
| 16.0.7 | webpack | 113.8 KB | ~16 KB |
| 16.3.5 (`latest`, 2026-09-11) | webpack | 127.4 KB | ~2.6 KB |
| 16.3.5 | turbopack | 129.9 KB | ~0.1 KB |

Next 16's client runtime is about 27 KB gzip larger than 15.5's (the router chunk grew ~18 KB, the
vendored React ~9 KB). **On Next 16.3 the framework alone is 98 % of the operator's budget**, so
≤ 130 KB is not reachable there whatever we do; on Next 15.5 it is reachable with room.

This table is left exactly as it was measured, before a line of the app existed — it is what the
operator decided on, and the decision below goes against its cheapest option on purpose.

Reproduce: `scratchpad/nextjs/` in the session that wrote this ADR, or
`frontend/customer/scripts/check-bundle-budget.mjs` against a build of this project.

## Decision

### Next.js 16.3, App Router, pinned exactly — the current major, chosen knowing the cost

**ADR 0028's ≤ 130 KB first load was reachable only on Next 15.5.** The table above is why: 15.5's
client runtime leaves ~30 KB for our code, 16.3's leaves ~2.6 KB. The app was first built and
measured on 15.5, where the overview came out at 109.8 KB gzip — inside the budget.

**Operator decision (2026-09-17): move to 16.3, the `latest` line, and accept the bytes.** Being on
the current major is what puts us first in line for the RSC security releases ADR 0028 (a) requires
within days for internet-facing code of ours; a `backport`-tagged line gets them later, and the
+27 KB would have to be paid on the day 15.5 stopped getting them anyway. The measurement is what
made the trade decidable rather than a preference, which is why it was taken before any code was
written.

`next` is pinned exactly — `"next": "16.3.5"`, no caret — so the number the budget check guards
cannot move on an install.

**The app's own code is unchanged by the version choice.** The screens, the BFF, the route handlers
and the islands are byte-identical between the two builds; nothing in this ADR's design depends on
the version. What the upgrade did touch is listed under "What Next 16 changed" below, and none of
it is behaviour a customer sees.

**Not chosen: TanStack Start** (ADR 0028's runner-up) — still unmeasured; it would keep the router
and query knowledge but loses the documentation advantage the operator weighted.

The conditions ADR 0028 (a) attached to Next.js hold: `NEXT_TELEMETRY_DISABLED=1`, no Vercel-only
features, the image optimiser unused (there are no images), and framework security releases in the
deploy runbook.

**No authorisation in middleware** (CVE-2025-29927 let a crafted header skip it entirely). There is
exactly one middleware and it does exactly one thing: mint a per-request CSP nonce. It decides
nothing about who may see what — the signed-in layout asks Django on every render. A nonce is
needed because the App Router streams its payload through inline `<script>` elements; without one
the page would need `script-src 'unsafe-inline'`, which is a step back from what ADR 0028 set for
the SPA. Caddy's own policy is therefore **not** applied to this upstream (it would replace the
nonce), and `make prod-surface-check` asserts that the app's policy survives and that the rest of
the security headers are still added.

The policy adds two sources the SPA also needs and did not have: `'wasm-unsafe-eval'`, without
which no browser instantiates the PDQ hasher (ADR 0017) that "add an original" uses, and
`media-src blob:` for the local file the decoder reads. **The Caddyfile's static policy was missing
both**, so browser fingerprinting would have failed on production under `script-src 'self'` alone;
that is fixed for the SPA path in the same change.

### What it measures out at

Fresh visit to each URL, gzip, modern browser. Both builds ship modules only; the Next build's
`noModule` polyfill chunk (38.5 KB) is excluded because no browser that can run the app downloads
it. Phase 1 is `frontend/scripts/check-bundle-budget.mjs`; phase 2 is
`frontend/customer/scripts/check-bundle-budget.mjs`, cross-checked against the **served HTML** of
the real app behind a stub Django.

**On Next 16.3 + webpack, what is shipped:**

| Route | Phase 1 (Vite SPA) | Phase 2 (Next 16.3) | |
|---|---|---|---|
| `/app` (overview) | 149.1 KB | **136.7 KB** | −12.4 |
| `/app/removals` | 147.7 KB | **136.7 KB** | −11.0 |
| `/app/invoices` | 146.7 KB | **136.7 KB** | −10.0 |
| `/app/originals/{id}` | 147.6 KB | **136.7 KB** | −10.9 |
| `/app/originals/{id}/copies/{id}` | 149.7 KB | **136.7 KB** | −13.0 |
| `/app/sign-in` | 144.9 KB | **137.9 KB** | −7.0 |
| `/app/rights` | 149.5 KB | **141.9 KB** | −7.6 |
| `/app/originals/{id}/notices` | 150.7 KB | **142.0 KB** | −8.7 |
| `/app/originals/new` | 150.4 KB | **142.3 KB** | −8.1 |
| shared floor | 143.8 KB (React + router + query + i18next + API client + one language) | 127.6 KB (React + the App Router client runtime) | −16.2 |
| CSS | 6.0 KB (both audiences in one stylesheet) | 4.4 KB | −1.6 |

**And what it would have been on the 15.5 line the measurement favoured**, kept here because it is
the price of the operator's decision and should stay visible: overview 109.8 KB, read routes
109.6 KB, sign-in 110.6 KB, rights 114.7 KB, notices 114.8 KB, add-original 115.2 KB, shared floor
100.3 KB. Every route about 27 KB cheaper, and inside ADR 0028's 130 KB.

So phase 2 on the current major is **7–13 KB better than the SPA per route**, not the 35–40 KB the
15.5 build was; ADR 0028's 130 KB is not met, and the gap is entirely Next 16's client runtime.
What it does buy over the SPA, at any version: the content paints from server-rendered HTML rather
than after React, a router and a query cache have booted; no resource file is JavaScript at all;
and the staff screens are not in the customer's build.

#### The budget the check enforces: 147 KB

`FIRST_LOAD_BUDGET_KB = 147` — the **worst measured route plus 4.15 KB**:

| | gzip |
|---|---|
| worst route, as the browser downloads it (`/app/originals/new`) | 142.33 KB |
| worst route, as the static check reports it | 142.85 KB |
| **budget** | **147 KB** |
| headroom over the static figure | 4.15 KB |

Why that much and no more: 4 KB is about what one of our screens grows by when it gains a form or a
table, so ordinary work does not trip the check; it is far less than any library (the smallest
thing we removed, i18next, was 14 KB), so nothing can join the shared graph unnoticed. It is not a
round number on purpose — the number's job is to fail on a regression, not to look tidy.

The static check is an **upper bound by 0.52 KB**: a route's client-reference manifest lists two
sibling chunks the runtime does not fetch (`app/layout`, 0.17 KB, and the overview's
`(signed-in)/page`, 0.35 KB). Over-reporting is the safe direction for a budget; the live figures in
the table above are the browser's.

**The per-route (30 KB beyond the shared entry) and CSS (15 KB) budgets are unchanged** and still
met with room — the worst route's own chunks are 10.9 KB, the stylesheet 4.4 KB.

Beyond the shared entry, the three form screens (rights, notice drafts, add an original) are the
large ones because they are client components from top to bottom. The fingerprinting code is in no
route's figure: it loads on "start" as two lazy chunks (3.8 KB for the decoder glue, 22.0 KB for
the PDQ WASM inside the Worker).

**Not measured here:** LCP, INP, CLS and TTFB (ADR 0028's other budgets). They need a browser and a
running stack; `make web-vitals` is still unbuilt, and the numbers above are the ones `make check`
can defend on every commit.

### What Next 16 changed

Five things, none of them in the screens, the BFF or the islands — those are byte-identical between
the two builds:

- **`middleware.ts` became `proxy.ts`** and the export `middleware` became `proxy` (Next 16
  deprecated the convention; `npx @next/codemod middleware-to-proxy` performed it). The same
  mechanism in the same position, so the rule stands: no authorisation lives there, because
  CVE-2025-29927 is not undone by a rename. The file says so.
- **Turbopack is Next 16's default bundler, and this app does not use it.** `npm run build` and
  `npm run dev` pass `--webpack` explicitly, for a reason that is not speed: **Turbopack cannot
  resolve `@shared/*`**, which points outside the project into `frontend/src`.
  `experimental.externalDir` is a webpack feature, and `turbopack.root`, an exact `resolveAlias`
  and a wildcard `resolveAlias` each still left 26 unresolved `@shared/…` imports (measured
  2026-09-17). Using Turbopack would mean duplicating ~2,100 lines of tested logic or moving them
  out of the SPA — losing the single shared copy that keeps the two customer apps from drifting
  while both exist. Webpack is also the smaller of the two (127.4 KB against 129.9 KB on the
  hello-world measurement), so the correctness argument and the byte argument agree. Revisit when
  the SPA's `/app` half is deleted and the shared modules can simply move into this project.
- **`eslint` is no longer a `next.config` key** (Next 16 dropped `next lint`). Removed; linting was
  already its own step in `make customer-check`.
- **Next rewrote `tsconfig.json`**: `jsx: "preserve"` → `"react-jsx"`, plus `.next/dev/types` in
  `include`. Accepted as the framework's mandatory change; it affects how `tsc` treats JSX, not
  what is emitted.
- **`import.meta.glob` collided.** Next 16 declares one of its own, with a different signature than
  Vite's, so `privacy.test.ts` and `lazyFingerprinting.test.ts` stopped typechecking. They now read
  the files with `node:fs` through `lib/sourceFiles.ts` — no bundler API at all, which is what
  `routes.test.ts` always did.

The budget script needed rewriting too: Next 16 no longer writes `app-build-manifest.json`, so it
reads `build-manifest.json` for the shared entry and each route's
`page_client-reference-manifest.js` for that route's own chunks. It still discovers the routes by
itself, so a new screen is measured without touching it.

### Where the server runs

- **Development:** `make customer-dev` runs `next dev` on `${WEB_BIND}:3000`, outside compose (the
  staff SPA's dev server works the same way). Server-side calls go to Django on
  `${CUSTOMER_DJANGO_BASE_URL:-http://127.0.0.1:8000}`.
- **Production:** a `customer` service in `infra/prod/compose.yaml` — `node:22-slim`,
  `output: "standalone"`, on the `edge` network, **no published port**, 384 MB limit (ADR 0028 f).
  Caddy is still the only thing the internet reaches and forwards `/app` and `/app/*` to it. It
  calls Django at `http://web:8000` on the same `edge` network.

The app's `basePath` is `/app`, so even its client chunks live at `/app/_next/static/*`: production's
public allowlist stays **exactly** the two paths ADR 0028 names, and Caddy needs no new route for
the framework.

```
browser ─https/h3─▶ Caddy ─┬─▶ /app, /app/*                  ─▶ customer (Next.js)   public
                           │      (incl. /app/_next/static/*)      │ edge network
                           │                                       ▼
                           └─▶ /api/v1/app/*, /api/v1/health ─▶ web (Django, customer surface)
```

### How it authenticates to Django: the browser's own session, forwarded

Django stays the authority (ADR 0028 c). There is **no new credential and no second session
store**: the Next server forwards the browser's `sessionid`/`csrftoken` cookies on every call and
forwards Django's `Set-Cookie` back. A customer's session is created by `POST /app/auth/login` as
ADR 0023 specifies — hashing, throttle, lockout, one answer for every failure — and the Next
server is a relay for it.

`lib/django.ts` (server-only; it throws if it is ever imported into a client component) sends:

| Header | Why |
|---|---|
| `Cookie` | the browser's session and CSRF cookies, verbatim |
| `X-CSRFToken` | on writes, from the browser's own header (double-submit stays the browser's) |
| `Origin: $CUSTOMER_PUBLIC_ORIGIN` | Django's CSRF check compares it with `CSRF_TRUSTED_ORIGINS` |
| `X-Forwarded-For`, `X-Forwarded-Proto` | **unchanged**, so the ADR 0023 login throttle still resolves the client, and `https` so Django keeps treating the request as secure |
| `Accept-Language` | the customer API may use it; the language of the *page* is decided here |

`CUSTOMER_PUBLIC_ORIGIN` is required in production and the server refuses the request without it:
a guessed origin is a CSRF check that passes for the wrong reason.

**Two things measured rather than assumed** (`infra/prod/check-customer.sh`, the app's own image
against a stub Django):

- **`Host` cannot be forwarded.** It is a forbidden header name in the Fetch standard and Node's
  `fetch` drops it silently — Django saw `web:8000`, not the public name. Production's
  `DJANGO_ALLOWED_HOSTS` therefore names the internal service as well as the public hostname
  (`dash.marceline.studio,web`). Nothing outside the private network can send either: Caddy
  answers only for `APP_DOMAIN`. Writing this down is the point; a header that looks set and is
  not is exactly how `ALLOWED_HOSTS` fails in production only.
- **`LOGIN_TRUSTED_PROXIES` stays 1.** Next's server does not add a hop to `X-Forwarded-For`, and
  the BFF forwards the header verbatim, so the rightmost entry is still the address Caddy saw. A
  second proxy count would have made every attempt count against this server instead of the
  customer; the smoke check asserts the one entry that arrives.

**Writes are route handlers, not server actions.** `POST /app/api/*` handlers forward one Django
endpoint each. Reasons: ADR 0028 (a) flags that Next checks a server action's `Origin` against the
host and that a proxied host must be exact; a route handler lets us do the check ourselves, in
code we can read and test (`Sec-Fetch-Site` must not be `cross-site`, and `Origin`, when present,
must equal our own origin), on top of Django's own CSRF check which is unchanged. They are also
plain HTTP, so they are testable without a browser.

**What this does not change:** `/api/v1/app/*` stays routed on the public host, Django's
`DJANGO_PUBLIC_SURFACE=customer` allowlist stays exactly as it is, and the 1 MB body cap with its
single 16 MiB exception for `POST /api/v1/app/fingerprints` stays on that path. Making Django
fully private (ADR 0028 d's dashed box) is deliberately **not** part of this step: it would remove
the rollback below and buys obscurity rather than authorisation (ADR 0028 b says so itself). It
stays open in the backlog. Caddy therefore gains one more capped path — the upload's own BFF
route, `POST /app/api/fingerprints`, at 16 MiB; every other `/app/*` request stays at 1 MB.

### Rendering, and what ships to the browser

- **Server components by default.** Every read screen — overview, original, record, removals,
  invoices, rights — is rendered on the server. `export const dynamic = "force-dynamic"` on the
  `/app` layout: every page is per-customer and nothing is cached or prerendered.
- **Client islands only where something happens:** the language switch, the sign-out button, the
  sign-in form, the submissions poller, the rights forms and "add an original". A disclosure
  panel is `<details>`, which needs no JavaScript at all — the staff SPA's `<details>` panels
  ported over for free.
- **An island gets its words as props**, from the server. No resource file is ever a client module,
  so the ~9 KB gzip language file that phase 1 had to ship is now zero bytes of JavaScript: the
  strings arrive inside the HTML, and only the ones that island uses.

### i18n: the same words, a 40-line translator, no i18next in the browser

`i18next` and `react-i18next` leave the customer app. The resources use exactly two i18next
features — `{{placeholder}}` interpolation and `_one`/`_other` plurals — and `Intl.PluralRules`
does the second one. `frontend/customer/lib/translate.ts` implements both in about forty tested
lines and runs unchanged on the server and inside an island.

This is a dependency dropped, so it is named as a decision: i18next's remaining value was runtime
language switching in a client-rendered app, and in a server-rendered one the language is a
property of the request. **The words themselves do not change and neither do their tests.**
`frontend/src/i18n/{en,de,language}.ts` stay where they are and stay the single copy; `i18n.test.ts`
(key parity, placeholders, plural sets), `wording.test.ts` (the customer wording rules) and
`translationKeys.test.ts` keep running in the Vite project over those files, and
`translationKeys.test.ts` now also scans `frontend/customer/` for `t("…")` keys.

Language per load, as today: the `marceline.language` cookie (same name the SPA used), then
`Accept-Language`, then English. The switch sets the cookie (a year, `SameSite=Lax`, not
`HttpOnly` — it is a preference, not a credential) and reloads the route, so a load speaks one
language and `<html lang>` is correct in the first byte. A returning customer whose choice is only
in `localStorage` is detected from `Accept-Language` once and re-pins it on the first switch.

### The pure logic and the words are shared, not copied

The Next project imports `frontend/src/i18n/{en,de,language}.ts` and the tested pure modules
`frontend/src/app/{apiTypes,caseFile,signInError,rights,invoices,submissions,addOriginal,notices,dashboard}.ts`
and
`frontend/src/lib/{videoFingerprint,fingerprintProgress,fingerprintClient}.ts` through
`experimental: { externalDir: true }` and a `@shared/*` path alias. One copy of the words and one
copy of every derivation, so the two apps cannot drift while both exist, and about 2,100 lines of
tested code — with the ~1,000 lines of tests that guard them — are reused rather than rewritten. Those modules now import `ApiError` from `src/api/errors.ts`
instead of `src/api/client.ts`, so pulling them in does not pull `openapi-fetch` (and its
`createClient()`) along; `client.ts` still re-exports it for the staff SPA.

`api/schema.d.ts` stays generated by `make frontend-api` and is imported for types only.

### Privacy and wording, enforced in the new place too

The customer UI still contains nothing *able* to load a remote image (issue #53).
`frontend/customer/` gets the same two enforcements the SPA's `src/app/**` has: the eslint block
and a source scan (`lib/privacy.test.ts`) covering `img`/`video`/`src`/`poster`/`backgroundImage`/
absolute URLs/`url()`/`/api/v1/media/`. The wording rules ("matching
copy" not "leak", "record / Vorgang" not "case", "human-reviewed match", "suggested notice
channel", no "infringing", no guaranteed times) are unchanged and still enforced by
`wording.test.ts` on the shared resources.

### Rollout and rollback: one Caddy variable

`infra/prod/Caddyfile` picks the customer upstream from `CUSTOMER_APP` with the snippet pattern it
already uses for `CADDY_TLS`:

- `CUSTOMER_APP=next` — `/app` and `/app/*` go to the `customer` container (its client chunks
  are under `/app/_next/static/*`, so no extra public path);
- `CUSTOMER_APP=spa` (the default when the variable is absent) — `/app`, `/app/*` and `/assets/*`
  are served from the Vite build inside the Caddy image, exactly as today.

So the cut-over is one line in `infra/prod/.env` plus a Caddy restart, and **the rollback is the
same line back** — no rebuild, no migration, no data change, and the Vite SPA's `/app` routes stay
in the repo and in the image until the operator has run on Next for a while. Both upstreams read
the same Django and the same session cookie, so a customer signed in on one is signed in on the
other. `make prod-config` checks that the `customer` service publishes no port, carries the public
origin and Django's private address, holds no key or database credential, and cannot reach the
database network. `make prod-surface-check` exercises the Caddyfile against **both** values.

Deleting the SPA's customer half (`frontend/src/app/**`, the `/app` routes in `src/router.tsx`, the
i18next dependencies) is a separate commit for after that, and it is what turns the shared imports
into a plain move.

## Consequences

- A second runtime on production: Node next to gunicorn, Postgres and Caddy. 384 MB limit; ADR
  0028 (f) budgeted it and it still fits a CX22.
- `make check` grows a third leg: `frontend/customer`'s typecheck, eslint, vitest, build and
  bundle budget. `frontend/customer/node_modules` is a second `npm ci` (`make customer-install`),
  which the check runner installs on demand. Two on-demand checks join it:
  `make customer-smoke` (the app's own image against a stub Django — the gate, the language, the
  CSP nonce, the write path) and the extended `make prod-surface-check` (the Caddyfile against both
  values of `CUSTOMER_APP`).
- The customer app's first load no longer depends on the Vite build at all, so the phase-1 budget
  check keeps guarding only what the staff SPA still serves.
- Two dev servers for someone working on both UIs (`make frontend-dev` and `make customer-dev`).
- `DJANGO_ALLOWED_HOSTS` on production gains the internal service name (`web`), because `Host`
  cannot be forwarded — see the measurement above. `LOGIN_TRUSTED_PROXIES` stays 1.
- A second image to build and ship on every release (`docker compose build customer`), and one more
  project whose security releases have to be applied promptly — which is the whole point of being
  on the current major.
- **The build is pinned to webpack, Next 16's non-default bundler.** That is a maintenance debt with
  a known payoff date: it ends when the SPA's `/app` half is deleted and the shared modules move
  into this project, at which point nothing resolves outside the root any more.
- The check runner reinstalls node_modules when a lockfile changes. It did not before, and this
  upgrade is how that was found: the first remote run built the new config against the cached Next
  15.5 and failed on `unknown option '--webpack'`. Better that than a green check for code nobody
  ran.
- Dates and numbers are now formatted on the server, in the request's language. `Intl` is in Node,
  so nothing is shipped for it — but a browser's own locale no longer influences them, which is
  what "one language per load" already implied.

## Amendment 2026-09-18: the words are loaded per screen, and each baseline is guarded (issue #99)

Both customer surfaces had arrived at their first-load guard, so the next sentence added to a
customer screen would have failed `make check`. Measured on the check runner at `origin/main`
70e9f3d, then after the change on `customer-first-load-trim`:

| Surface / route | before | after | |
|---|---|---|---|
| **SPA `/app` first load** (entry + shell + words + overview) | **149.88 KB** | **142.65 KB** | −7.23 |
| — of which the words | 11.46 KB (one whole language file) | 3.86 KB (that language's core slice) | −7.60 |
| — of which the framework entry | 105.75 KB | 105.79 KB | +0.04 |
| — of which the customer shell | 27.42 KB | 27.75 KB | +0.33 |
| **Next `/app/originals/new`** (the worst route) | **146.43 KB** | **145.62 KB** | −0.81 |
| Next `/app/originals/{id}/notices` | 145.53 KB | 145.55 KB | +0.02 |
| Next `/app/rights` | 145.48 KB | 145.50 KB | +0.02 |
| Next read routes, `/app` | 139.35 KB | 139.38 KB | +0.03 |
| Next shared entry (the framework floor) | 127.56 KB | 127.58 KB | +0.02 |

Headroom under the unchanged 150 KB guard: SPA **0.12 KB → 7.35 KB**, Next app **3.57 KB →
4.38 KB**.

**Where the weight actually was.** Not where a reader of the two budget scripts would guess:

- In the **Next app** there is almost nothing of ours to trim. 127.58 KB of the worst route's
  145.62 is the shared entry — React plus the App Router client runtime, against the 127.4 KB
  hello-world this ADR measured on the same major — and 6.49 KB is the stylesheet. Our own client
  JavaScript is 5.31 KB on a plain route and 11.55 KB on the worst one, of which **3.52 KB is
  `next/link` and the router internals `Nav` pulls in**, on every route. And no resource file
  reaches a client chunk at all: grepping every built chunk for a UI string from `en.ts` finds
  nothing, because the words arrive as props from the server. The one real find was that a static
  import of `DEFAULT_FPS` / `MAX_UPLOAD_POSITIONS` / `stem` dragged the whole fingerprint document
  builder and validator (2.7 KB raw) into `/app/originals/new`'s first load, although it only runs
  after the customer presses start: the format constants moved to
  `@shared/lib/videoFingerprintFormat`, and `asRequestBody` now arrives with the hasher.
- In the **SPA** the words were the weight. It loaded **one whole language file** on every `/app`
  visit while the overview names about a sixth of it, and `rights`, `notices`, `caseFile` and
  `addOriginal` — four fifths of the resource — belong to one route each. They are slices now
  (`src/i18n/{en,de}/*.ts`, `src/i18n/slices.ts`), loaded in the route's own TanStack loader;
  `en.ts` / `de.ts` compose them for the tests and for this app, which holds the whole resource on
  the server anyway. Three sections are split across the core slice and a screen's slice, because
  the shell and the overview name a few of their keys (`caseFile.where.open`,
  `addOriginal.submissions`, `rights.works.missing` and `.blockers`); **no key path changed**, and
  `i18n.test.ts` asserts every key lives in exactly one slice.

That also **ends ADR 0044's exception**: the cloud-upload words no longer need a module outside
`en.ts` / `de.ts` (`src/i18n/upload.ts`, deleted) — they are part of the `addOriginal` slice, so
they cost the one route that shows them and nobody else.

**Two new baseline guards**, because a trimmed screen otherwise makes room for something worse:

| Guard | Where | Number |
|---|---|---|
| framework shared entry | `frontend/customer/scripts/check-bundle-budget.mjs` | `SHARED_ENTRY_BUDGET_KB = 128.5` |
| framework entry graph | `frontend/scripts/check-bundle-budget.mjs` | `ENTRY_BASELINE_KB = 107` |
| the words every visitor pays for | same | `CORE_WORDS_BUDGET_KB = 4.5` |

A Next upgrade that costs bytes, a library joining a shared graph, or a screen's words put in
`core.ts` now fails on its own line and says which one it was, instead of showing up as "the total
grew". The SPA's script also fails if anything shipped imports the composed `en.ts` / `de.ts`
statically, which would silently undo the split, and `src/app/translationKeys.test.ts` fails if a
screen names a key from a slice its route does not load — the one mistake this design makes
possible, and the one that would render a key to a paying customer.

**The budgets themselves were not raised.** The operator's decision of 2026-09-18 was to trim first
and re-decide the number once the real headroom was known; both scripts still guard 150 KB.

**And the number he then re-decided is the per-route one: 30 KB → 16 KB** (operator, 2026-09-18),
in both scripts. The reason is the measurement above: the first-load total is dominated by a
framework floor we do not control, so it is a poor alarm on our own code — it cannot fail on a
screen doubling in size. The per-route number is the one that measures us, and at 30 KB it guarded
nothing: the worst route spends **11.55 KB** of its own in the Next app and 10.71 KB in the SPA, and
none has ever been close to 30. 16 KB is the worst route plus about 4.5 KB — roughly what a screen
grows by when it gains a form or a table — so ordinary work still passes and a screen that doubles
does not. The 150 KB totals stay where they are, with the baseline guards below them.

## Alternatives rejected

- **Next 16.3 with a 135 KB budget.** Honest, current, and ~14 KB better than the SPA — but it
  does not meet the number the operator set, and the difference is the framework, not us. Left as
  the operator's decision above rather than taken silently.
- **Keeping i18next on the server only.** It would work and cost nothing in the browser, but the
  islands still need words, and shipping a second mechanism for them is worse than one translator
  used by both. The resources and their tests are untouched either way.
- **Duplicating the words and the pure logic into the Next project.** The fastest merge and the
  worst outcome: two copies of ~2,100 tested lines, drifting while both apps exist, with the
  wording test watching one of them.
- **Server actions for the writes.** Less code, but an `Origin` check we do not control behind a
  proxy (ADR 0028 a), an RPC surface with no URL to test, and a 1 MB default body limit exactly
  where the 16 MiB upload goes.
- **A `/app/api/*` catch-all proxy to Django.** One handler instead of twenty-one, and a public
  tunnel to whatever `/api/v1/app/*` grows next. Each call gets its own named handler, and a test
  matches every path an island sends against the handlers that exist.
- **Making Django private in the same step.** ADR 0028 (d) plans it; doing it here would delete the
  rollback and mix an authorisation-neutral change (b) into the release that has to prove the load
  time.
- **Serving `/app/_next/static/*` from Caddy precompressed** (ADR 0028 d). Worth doing, needs the build
  output copied into the Caddy image; Caddy's `encode zstd gzip` in front of the Next server gets
  most of it. Left open in the backlog.
