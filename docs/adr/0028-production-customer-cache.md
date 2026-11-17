# 0028 — Production is a small read-through cache in front of the development VPS

Status: **accepted** (2026-09-17, operator) — the read-through cache, `dash.marceline.studio` as
the customer app's hostname, `cryptography` and `sentry-sdk` as dependencies, freshness 15 minutes
plus instant purge on safety, Sentry in the EU region, and the Next.js customer frontend as the
**phase 2** plan below (not now). The first slice is built (see "Implementation, phase 1" at the
end). Issue #37; changes the shape of #38. Amends ADR 0005.

## Context

ADR 0005 put web + Postgres on one EU VPS and the crawler on a separate small one. The operator
reversed the emphasis on 2026-09-16:

> Prepare everything possible on this VPS, but with the goal to separate the production deployment
> onto a different, smaller VPS. Production is for stability and availability but has limited
> resources and only a **data cache** — the minimum needed to operate, because development and
> analytics may have downtime. The development VPS is where the crawlers run, new features are
> tested, and the **main database** lives. […] We must save money and expand only once revenue
> covers running costs.

The first draft of this ADR proposed a signed snapshot pushed from development over SSH. The
operator's feedback on 2026-09-17:

> Keep the signature, but I don't get why we use SSH in the first place. I thought we'd do it more
> like a cache which builds up on prod and gets invalidated after x min or when dev
> force-invalidates.

and: "when the customer API is public, require CORS — only the frontend should be able to make
requests". Further decisions that day: freshness 15 minutes plus instant invalidation on a safety
block; Sentry in the EU region; a free external uptime check on `/api/v1/health`; production on
Hetzner Cloud (CX22 class); the development VPS moves to Hetzner too after the Netcup contract year.
The operator then raised a server-rendering frontend with a non-public backend, leaning towards
Next.js, and made **load time a top requirement** ("better than Atlassian and Microsoft") — see
"The customer frontend" below.

Budget: about €150/month in total. What a customer needs is small — sign in, see their originals,
findings, cases, removals and hosting sections: a few megabytes per organisation. Everything
expensive (crawling, hashing, matching, capture, review) is internal and may be down for an
afternoon without a customer noticing, provided the customer's view is served from somewhere else.

Constraints:

- **Safety-flagged pages never reach a customer** (ADR 0011, ADR 0018) — not from a stale cache
  either.
- **Tenancy is decided in one place** (ADR 0021: `marceline/app/ownership.py`, the 404 rule). A
  second implementation of it on production would be the way two customers get confused.
- **A production compromise should yield as little as possible**, and nothing staff-facing.

## Decision

### Two machines, two roles

| | Development VPS (internal) | Production VPS (public) |
|---|---|---|
| Purpose | main database, crawling, hashing, matching, review, experiments | serve customers; stay up |
| Runs | `infra/compose.yaml`: Postgres, web (full surface), workers, scheduler, capture; later the **export** listener | `infra/prod/compose.yaml`: Caddy, gunicorn (customer surface), Postgres (accounts + cache) |
| Reachable | **Tailscale only**; never a public port | 80/443 public; SSH and the purge listener over Tailscale only |
| Staff UI, admin, review API | yes, over Tailscale | **no** — refused by Caddy and by Django |
| Size / cost | as today | 2 vCPU / 4 GB / 40 GB, Hetzner Cloud CX22 class, about €4–6/month |

Production has **no staff accounts**; an operator who needs something there uses SSH and
`manage.py`. The customer app no longer shares a host or an IP with the crawler, which settles #38
without a third machine: abuse complaints or blocks against the crawler's address cannot take the
product down, and no public port ever opens on the crawler host.

### Production is a read-through cache of whole customer responses, one entry per organisation

**What is cached: the rendered customer API responses, grouped per organisation** — not database
rows. On a cache miss, production does not ask for one endpoint's answer; it asks development for
the organisation's **bundle**: every customer document that organisation can currently request,
rendered by the same `marceline/app` code that serves `/api/v1/app/*` on development today:

- `originals`, and `originals/{id}` for every original it owns;
- `findings`, `cases`, `cases/{id}` for every case, `removals`.

The customer API has no query parameters, so this set is finite and small. `me`, `auth/session`,
`auth/login` and `auth/logout` are answered by production itself from its local account copy (below).
An id that is not in the organisation's bundle answers 404 with the exact body development would
give — unknown and foreign stay indistinguishable (ADR 0021).

Why whole responses, per organisation:

- **Tenancy and safety stay single-sourced.** Every rule that decides what a customer may see —
  ownership, confirmed-only (ADR 0025 amendment), flagged pages never listed or counted, no media,
  hosting reduced to hostnames — runs on development, in the code and tests that exist today.
  Production only stores and returns bytes it was given for *that* organisation. A row cache would
  need the schema subset, the joins and the filters again on production, and a second place to get
  them wrong.
- **Useful when development is down.** One successful fetch gives production every screen of that
  organisation, not just the page that was open. A per-endpoint cache would leave a customer who
  logged in during an outage with a half-working app.
- **No schema coupling.** The bundle's documents *are* the public API contract (`openapi.json`),
  which already has drift checks. Production needs none of the matching, index or catalog tables.
- **Cheap.** A bundle is kilobytes to a few megabytes. Rendering one on development is the same
  work as a customer opening every screen once.

### The flow

```
browser ──https──▶ Caddy ─▶ gunicorn (prod, customer surface)
                               │ bundle(org) fresh?  ── yes ──▶ answer from cache
                               │ no / missing
                               ▼
                   GET https://dev-export (Tailscale) /internal/v1/export/organisations/{org}
                     Authorization: scoped service token   ◀── signed bundle (Ed25519, dev key)
                               │ verify signature, org id, freshness, schema version
                               ▼
                         store, answer            (fetch failed → serve stale, below)

dev ──Tailscale──▶ POST prod-internal /internal/v1/cache/purge   (signed by dev: org ids / page ids)
```

1. **TTL: 15 minutes**, a constant (`CUSTOMER_CACHE_TTL`). A request for an organisation whose
   bundle is older fetches a new one synchronously with a short timeout (3 s); concurrent requests
   for the same organisation wait on one fetch (a Postgres advisory lock per organisation) or, if
   a stale bundle exists, are answered from it immediately.
2. **Serve stale on error.** If development does not answer, answers an error, or sends something
   that fails verification, production answers from the last good bundle and records the failure.
   **Maximum staleness shown: 72 hours** (`CUSTOMER_CACHE_MAX_STALE`) — long enough to ride out a
   weekend outage or the move of the development VPS to Hetzner, short enough that a removal state
   is never presented as current for days. Past it, and on a miss with development down, the
   answer is **503 `data_temporarily_unavailable`**, never an empty list: an empty list would read
   as "nothing found".
3. **The UI says how old it is.** Every cached answer carries `X-Data-As-Of` (the time development
   rendered it — part of the signed envelope, not the fetch time). The customer shell shows
   "Stand: …" / "Last updated …" whenever that is older than the TTL, so stale data is never
   presented as live. (A small change in `frontend/src/app/`, owned elsewhere.)
4. **Force invalidation from development.** Development calls
   `POST /internal/v1/cache/purge` on production's Tailscale-only listener with organisation ids
   and/or page ids, signed with the development key (with a timestamp and nonce against replay).
   Two modes:
   - `expire` — after publishing decisions (a leak confirmed, a case changed, a hosting refresh):
     the next request refetches; if development is down meanwhile, the old bundle is still served.
   - `delete` — **immediately on a safety block** of a page that appears in any bundle: the
     entries are deleted, not expired, so a stale copy can **never** be served again. If
     development is down at that moment the customer gets a 503 rather than the flagged page;
     safety wins over availability. Production keeps the page ids each bundle contains, so a
     page purge finds its organisations without asking development.

   Development records each purge in a small outbox and retries until production acknowledges;
   the TTL bounds an `expire` that is lost. A `delete` for a safety block additionally makes the
   export refuse that page from the moment it is flagged, so a refetch cannot bring it back.
5. **Warm-up.** A rebuilt production starts cold; the first request per organisation needs
   development. No background pre-fetcher in phase 1 (it would be another process on a small
   machine); revisit if cold starts after deploys become visible.

### What the signature protects

Every bundle (and every purge) is an envelope `{organisation_id, as_of, expires_at, api_version,
documents, page_ids}` signed by development with an Ed25519 key only development holds; production
has only the public key. Tailscale already authenticates and encrypts the path, so the signature is
defence in depth against the cases Tailscale does not cover:

- **a spoofed or compromised development endpoint** — another tailnet node that obtains the export
  address (a misconfigured ACL, a stolen device, MagicDNS pointing somewhere else), or a proxy in
  between — cannot make production store or serve data it invents;
- **a keying bug**: the organisation id is inside the signature, so a bundle for organisation A can
  never be filed under B, even if the cache code gets its keys wrong;
- **replay of old data** (`as_of` must not go backwards; `expires_at` bounds use) and **forged
  purges** (a purge that deletes every entry is a denial of service).

It does not protect against a fully compromised production host, which can serve whatever it
likes — nothing on the receiving side can.

### The development export API

- A **separate listener** on development (`DJANGO_PUBLIC_SURFACE=export`, a third surface next to
  `full` and `customer`), bound to development's Tailscale address on its own port. Its allowlist
  is `/internal/v1/export/…` only; the staff API and admin are not on that port at all.
- **Tailscale ACL**: only the production node may reach that port; development may reach only
  production's purge port. Operator devices keep SSH to both.
- **Scoped service token** (bearer, stored hashed on development, rotated by the operator), which
  opens exactly two read-only operations: `GET /internal/v1/export/organisations/{id}` (the bundle)
  and `GET /internal/v1/export/accounts` (below). Rate-limited per token.
- **Read-only at the database level**: the export process connects as a Postgres role with
  `SELECT` only. Case reconciliation, which today writes on read (ADR 0025), runs as a development
  scheduler job instead (every few minutes, and before a purge after decisions); the export path
  sets reconciliation off. That switch lives in `marceline/app/cases.py` (owned elsewhere).

### Accounts, sessions and other writes on production

| Data | Lives | How it gets there |
|---|---|---|
| Users (username, password hash, active), organisations, memberships | authoritative on **development** (admin, `changepassword` — ADR 0023 has no self-service) | production pulls a signed `accounts` document every 15 minutes and on an `accounts` purge; kept locally so **logins work while development is down**. Staff flags are never exported; only users with a membership are |
| Sessions, login attempts, lockouts, `last_login` | **production only** | never synced; lost on a rebuild (customers sign in again) |
| Unlocking a lockout | production (`manage.py unlock_login` over SSH) | — |
| Later: a customer's case choices, customer uploads of originals, consents | a production **outbox** (append-only, validated events) | development pulls it over the same Tailscale path with its own token and acknowledges; not built in phase 1 — **built for uploads on 2026-09-17** with signed pulls instead of a token (amendment "The production write path") |

Logins never trigger a request to development: an unknown username must not become a way to make
production hammer development, nor a timing oracle.

### "Only the frontend may call the API": no CORS, CSRF on every write

The operator asked to require CORS so that only our frontend can make requests. The SPA is served
from **the same origin** as the API (`https://dash.marceline.studio/app` and `/api/v1/app/…`), so the correct
configuration is **no CORS headers at all**:

- Without `Access-Control-Allow-Origin`, a browser page on any other origin cannot read a response
  from our API, and a non-simple cross-origin request (JSON body, custom header) is stopped at the
  preflight. Adding CORS headers could only *widen* who may call us; there is nothing to "enable".
  `marceline/tests/test_cross_origin.py` asserts that no customer operation — preflight or request,
  development or customer surface — carries any `Access-Control-*` header, and that no CORS package
  is installed; `make prod-surface-check` asserts the same at the Caddy edge.
- **CSRF on every state-changing customer endpoint** (the token cookie echoed as `X-CSRFToken`,
  plus Django's Origin check): the test walks every unsafe `/api/v1/app` operation and requires a
  403 without the token, and a 403 with a valid token sent from a foreign `Origin`.
- **Cookies**: session cookie `Secure`, `HttpOnly`, `SameSite=Lax`; CSRF cookie `Secure`,
  `SameSite=Lax`, readable by the SPA on purpose. `Lax` rather than `Strict`, because `Strict`
  drops the session on a top-level navigation from another site — a customer following a link
  from our own notification mail would appear signed out — while unsafe methods are refused
  cross-site either way.
- **`CSRF_TRUSTED_ORIGINS` names only the app's own origin**; production refuses to start with an
  origin whose host is not in `ALLOWED_HOSTS` (`core/deploy.py`).

**What this cannot do:** CORS, CSRF and SameSite are *browser* rules. They do not stop curl, a
script or another server from calling the API. What stops those is authentication (a session only
our login creates), organisation scoping with the 404 rule, and the login throttle. A signed-in
customer can always script requests with their own session — they can only reach their own data,
and that is acceptable.

### The customer frontend: Next.js in front of a private API (backend-for-frontend)

The operator raised this on 2026-09-17: "It is possible to protect an API from unauthorized access:
our frontend could do mixed server- and client-side rendering and our real backend is not public."
Later the same day the operator leaned towards **Next.js** (App Router, mixed server/client data
fetching, well maintained by agents), and set **load time as a top requirement** ("better than
Atlassian and Microsoft"). A non-public backend makes the API less obvious to intruders; that is
not counted as security.

Today the customer app is part of the Vite SPA and calls `/api/v1/app/*` from the browser. Under
this option a **Next.js server** on production renders the customer pages and is the only thing
the internet reaches; Django's API is bound to production's private Docker network (and to
Tailscale on development) and is never public. It combines with the read-through cache without
changing it:

```
browser ─https/h3─▶ Caddy ─┬─▶ /_next/static/* (files, immutable, precompressed)
                           └─▶ Next.js server (server components, route handlers, server actions)  public
                                   │ private network: Django session + service credential
                                   ▼
                             Django on prod (customer API, accounts, sessions, cache)             private
                                   │ Tailscale, export token, signed bundles
                                   ▼
                             Django export on dev                                               private
```

**Scope:** only the **customer app** moves to Next.js. The staff moderation SPA stays Vite, stays on
development, stays Tailscale-only — and stops being shipped to the internet at all. Today's public
build is one 556 KB JavaScript file (169 KB gzip, measured 2026-09-17) that carries the staff
screens, the fingerprinting client and the customer app together; every customer downloads all of
it.

#### (a) Framework: Next.js proposed, TanStack Start runner-up

- **Next.js (App Router), proposed.** Server components by default, client components only where
  interactive, streaming, route-level code splitting, a self-hostable Node server
  (`output: "standalone"`), and the largest body of documentation and examples — which is what
  keeps agent-maintained code conventional. **No hard blocker found.** Conditions that come with it:
  - telemetry off (`NEXT_TELEMETRY_DISABLED=1`), no Vercel-specific features, the image optimiser
    unused (there are no images);
  - **never rely on Next.js middleware for authorisation** — CVE-2025-29927 (2025) let a crafted
    header skip middleware entirely. Django decides access on every call; Next.js only renders;
  - the React Server Components server is now internet-facing code of ours: framework security
    releases (RSC had a critical remote-code-execution advisory in late 2025) must be applied within
    days, which belongs in the runbook's update routine;
  - Next.js checks the `Origin` of server actions against the host; behind Caddy the forwarded
    host must be exact (tested), and route handlers that change state need the same check by hand.
- **TanStack Start, runner-up.** It keeps the router and query layer we use today, so the move would
  be smaller, and its baseline is lighter. It loses on maturity and on the amount of reference
  material agents can lean on, which is exactly what the operator weighted.
- **Django templates:** no Node on production at all, but every customer screen rewritten, the
  React components and their tests lost, and i18n moved to Django's catalogue — most work, least
  reuse. **React Router framework mode (Remix):** a sound SSR framework, but it replaces the router
  like Next.js does without Next.js's ecosystem advantage.

#### (b) What it protects, and what it does not

- The **backend and its endpoint inventory are hidden**: no `/api/v1/…` on the internet, no
  OpenAPI shape to probe; Django's allowlist middleware becomes the second line.
- **Service credentials stay server-side**, and responses are **shaped per page** — a page receives
  the fields it renders, not a general-purpose resource.
- **Not** protected: Next.js's own route handlers, server actions and RSC requests are HTTP
  endpoints. A scripted client with a valid session cookie can call them exactly as the browser
  does, and their identifiers are visible in the page. They still need session authentication, CSRF
  / Origin checks on every mutation, per-organisation scoping in Django and rate limits. A BFF
  reshapes and narrows the surface; it does not replace authorisation. "Only our frontend can call
  it" stays true for browsers (same origin, no CORS) and false for anyone holding a session.

#### (c) Auth, sessions and the 404 rule

- **Django stays the authority (proposed).** Next.js forwards the login to Django over the private
  network; Django creates the session exactly as today (ADR 0023: hashing, throttle, lockout, one
  answer for every failure). Next.js sets it on the app domain as an `HttpOnly`, `Secure`,
  `SameSite=Lax` cookie and forwards it on each private call together with a service credential
  that proves the call comes from the Next.js server (so Django's customer API refuses anything
  that reaches it some other way). The client IP is forwarded for the login throttle, with
  `LOGIN_TRUSTED_PROXIES` counting Caddy and Next.js. No new token scheme, no second password store.
- Rejected: Next.js-owned sessions forwarding a signed user/organisation claim — login throttling
  re-implemented in Node, and a frontend server that can assert any organisation.
- **The 404-not-403 rule survives** because scoping stays in `ownership.owned_references`. Next.js
  renders one not-found page for every upstream 404 (`notFound()`), never turns an upstream refusal
  into a distinguishable message, and a test asserts that an unknown and a foreign id render
  byte-identical responses, as `test_app_tenancy.py` does for Django.

#### (d) Performance budget and how it is met

Targets, measured **on production** with Lighthouse's mobile profile (mid-range phone, simulated 4G:
150 ms RTT, 1.6 Mbit/s, 4× CPU slowdown) and later with real-user data at the 75th percentile:

| Metric | Budget | Aim |
|---|---|---|
| Largest Contentful Paint | ≤ 1.5 s | 1.0 s (field, p75) |
| Interaction to Next Paint | ≤ 100 ms | |
| Cumulative Layout Shift | ≤ 0.05 | 0 |
| Time to First Byte, cached organisation | ≤ 200 ms | ≤ 100 ms from Germany |
| Client JS, route-specific (per route, gzip) | ≤ 30 KB | overview ≤ 20 KB |
| Client JS, first load in total (gzip) | ≤ 130 KB | |
| CSS (gzip) | ≤ 15 KB | |
| Fonts | 0 bytes: system font stack | |
| Third-party requests on customer pages | 0 | |

**Plainly: a total first-load budget of about 90 KB gzip is not reachable with Next.js.** React 19
plus the App Router client runtime is already around 100 KB gzip before our first component (measure
at adoption: `next build` prints "First Load JS shared by all"). The budget therefore splits into a
fixed framework share and a strict **per-route** share, which is the part we control. That is not a
contradiction of the load-time goal: with server rendering and streaming the content paints from
HTML before any JavaScript runs, so LCP depends on TTFB and HTML size, and JavaScript size mostly
affects hydration and INP. If the total must go below ~90 KB, the options are TanStack Start
(lighter), or islands with Preact — both weaker on the operator's other criteria.

How the budget is met:

- **Server components by default**; client components only for interaction (language switch,
  sign-in form, disclosure widgets). Data is fetched on the server; no TanStack Query in the
  browser for first render.
- **Streaming**: the shell and the first screenful flush immediately; slower sections stream in
  under `Suspense` with fixed-size placeholders (CLS).
- **Data is local**: Next.js → Django on the same machine → the read-through cache. A cached
  organisation needs no network hop to development, which is what makes TTFB ≤ 200 ms realistic.
- **Caddy serves `/_next/static/*` directly** with `Cache-Control: immutable` and precompressed
  brotli/zstd/gzip files produced at build time (`file_server precompressed`; Caddy's standard
  build cannot brotli-compress on the fly). HTTP/3 is already on. Hetzner in Germany keeps RTT low
  for the first (German) customers.
- **No images, no web fonts, no third-party scripts.** The customer UI shows no pirate imagery
  (#53) and uses the system font stack.
- **Sentry stays server-side** (Django and the Next.js server). The browser SDK costs roughly
  25–30 KB gzip and a third-party connection on every page; client errors are instead posted, with
  a few lines of our own code, to our own backend.
- **Real-user monitoring later, first-party only:** the `web-vitals` library (about 2 KB) sends
  LCP/INP/CLS/TTFB, sampled and without user or organisation ids, to a small endpoint of our own.

Enforcement:

- **In `make check` (cheap, no browser):** a bundle-size check over the Next.js build output —
  per-route and total gzip sizes against the table above, failing the build on a regression (e.g.
  `size-limit` or a 40-line script reading the build manifest).
- **On demand (`make web-vitals`, not in `make check`, like `make browser-parity`):** Lighthouse in
  headless Chromium with the mobile profile against the rehearsal stack or production, reusing the
  Playwright/Chromium install from `frontend/parity`; fails when a budget is exceeded. Run before
  every release that touches the customer app.
- **Later:** the first-party web-vitals endpoint, and a weekly p75 report.

#### (d.1) Phase 1 interim: the same budget on the Vite SPA (2026-09-17, issue #82)

The first production deploy served `/app` from the phase 1 Vite SPA's single bundle: one 620 KB
(**188 KB gzip**) script carrying the staff moderation screens, the fingerprinting client and
**both** languages together — every `/app` visitor downloaded all of it, staff screens included.
Fix, without moving to Next.js yet:

- **Every screen is a lazy route component**, not just `AddOriginalScreen`
  (`frontend/src/router.tsx`): staff screens (`ReferencesScreen`, `ClustersScreen`, …) and customer
  screens alike, plus the staff `RootLayout` and the customer shell `CustomerApp` itself. A
  customer visiting `/app` never fetches a byte of the staff screens or their `GET /auth/me`; a
  staff URL never fetches the customer app.
- **One language loads, not two.** `src/i18n/index.ts`'s `ensureI18nReady()` dynamically imports
  only the detected language (`en.ts`/`de.ts`, ~600 lines each) and initialises i18next with it;
  the `/app` route's TanStack Router `loader` awaits it before any screen renders. Switching
  language loads the other one, once, the first time someone actually clicks it
  (`useLanguage`'s setter). Both files stay statically imported only from
  `src/i18n/resources.ts`, a test-only module nothing shipped imports.
- **The typed API client no longer rides along with `main.tsx`.** `ApiError` and its formatting
  moved to `src/api/errors.ts` (no `openapi-fetch` import, so no `createClient()` side effect);
  the staff query keys moved to `src/api/queryKeys.ts` — both tiny, side-effect-free modules the
  shared entry chunk can import without pulling in `openapi-fetch` or the staff `queries.ts` for
  every visitor.

Measured (`frontend/scripts/check-bundle-budget.mjs`, gzip, from the Vite build manifest):

| | Before | After |
|---|---|---|
| `/app` first load (staff + customer + both languages, one bundle) | 188 KB | — |
| `/app` first load (shared entry + customer shell + one language + the overview) | — | **~145 KB** |
| Largest additional `/app` route (`AddOriginalScreen`, incremental) | — (all in the one bundle) | **~6.5 KB** |

**ADR 0028's ≤ 130 KB first-load figure is not reached, and the reason is structural, not a missed
optimisation.** Measured in isolation with this project's own `vite build`: `react` + `react-dom`
(production, client) alone is **~68.5 KB gzip**; `@tanstack/react-router`'s core (no devtools) adds
**~26.3 KB**; `@tanstack/react-query`'s core adds **~7 KB**. That is a ~102 KB floor before
i18next, the API client or a single component of ours — full client-side rendering pays for React,
the router and the query cache on every visit, where section (d)'s Next.js plan pays for almost
none of it (server components render the HTML; the client ships React only for the interactive
islands). This is the same wall section (d) hit for Next.js's own ~100 KB floor, one rung further
down: **a target chosen for a server-rendered app does not fit a fully client-rendered one, on
either framework.** Closing it needs phase 2 (server components, no client router or query cache
shipped for most of the page) or a materially lighter router/query stack — either is a bigger
change than splitting the existing SPA.

`make frontend-check` runs `frontend/scripts/check-bundle-budget.mjs` against the built manifest
and fails on a regression: **150 KB** first load (today's ~145 KB plus slack — the phase 1 interim
ceiling, not ADR 0028's 130 KB, for the reason above) and ADR 0028's own **30 KB** per additional
route, which phase 1 already meets with room (largest route ~6.5 KB). The script's own comments
carry this same explanation so it is not lost between here and the code.

#### (e) Migration effort, i18n and tests

Today's customer app is about 4,600 lines in `frontend/src/app/` and `src/i18n/`, roughly half of
it tests. Estimate: **one to two weeks of agent work plus review.**

| Part | Lines today | Fate |
|---|---|---|
| `en.ts` / `de.ts` resources, `signInError.ts`, `caseFile.ts` (pure logic) | ~1,070 | reused as they are |
| Their tests (`i18n`, `translationKeys`, `signInError`, `caseFile`, `privacy`) | ~550 | reused; the privacy scan points at the new directory |
| Screens (`HomeScreen`, `OriginalScreen`, `CaseFileScreen`, `RemovalsScreen`, `SignInScreen`, `parts`, `LanguageSwitch`) | ~1,160 | markup and Tailwind classes reused; data loading, routing and links rewritten (server components, `next/link`, route params) |
| `CustomerApp`, `queries.ts`, `routeIds.ts` | ~280 | replaced by the App Router layout, server-side fetch helpers and the not-found handling |
| Screen and app tests (`customerApp`, `customerScreens`, `caseFileScreen`, `testApi`) | ~1,340 | rewritten: server components are tested by rendering with a stubbed Django client; interactive parts keep Testing Library |

i18n: i18next runs on the server too, but the language comes from the request (cookie, then
`Accept-Language`) instead of `localStorage`, and each request gets its own instance so two
customers never share one. The German/English parity tests stay.

Build and repository: a separate `customer-web/` (or `frontend/customer/`) Next.js project with its
own `package.json`, lint and vitest config, added to `make check`; the API types keep coming from
`openapi.json`; images are built on development as today.

#### (f) Cost on a CX22 (2 vCPU, 4 GB)

A Next.js standalone server uses roughly 120–250 MB resident at our traffic; give it a 384 MB
limit. Next to Postgres (1 GB), gunicorn (768 MB, which can shrink to about 512 MB once Django only
serves private calls) and Caddy (128–256 MB), the limits total about 2.3–2.4 GB of 4 GB: it fits,
with room for the OS and page cache. Server rendering costs CPU per page view, which is negligible
for tens to hundreds of customers; Node starts in about a second and runs as a long-lived
container, so there is no cold start per request. The static-SPA image goes away; a Node image
takes its place.

#### (g) Recommendation and decision point

- **Phase 1 (now, built): the Vite SPA on the same origin**, no CORS, CSRF on every write, customer
  surface allowlisted in Caddy and Django, behind the read-through cache — for the first test
  customers while the customer screens still change daily.
- **Phase 2 (before public launch): the Next.js customer app with a private Django**, the
  performance budget enforced from its first commit, and the staff code out of the public build.
  Start it once the customer screens settle; triggers that pull it earlier are a server-side
  secret the UI needs, a security review, or any marketing or self-service signup.
- Building it immediately is possible but not cheap: it would stall the screens being built now and
  put a second runtime on the smallest machine before a customer uses it.

**Operator decision (2026-09-17):** Next.js as the customer frontend is the accepted **phase 2**
plan, with the budget table above; it is not built now. Phase 1 (the Vite SPA behind the cache)
ships first.

### The production stack

- **Caddy** (`infra/prod/Caddyfile`): automatic HTTPS for `dash.marceline.studio`, HSTS, CSP (plus
  `frame-ancestors 'none'`), no Referer, no `Server`, no admin endpoint, no access log. Public
  routes: `/api/v1/app/*`, `/api/v1/health`, `/app`, `/app/*`, `/assets/*`, `/` → `/app`;
  **everything else 404**, including `/internal/*` and the staff SPA routes. The purge endpoint
  has a second site block (`http://:8081`) whose port compose publishes on production's Tailscale
  address only.
- **Django's own wall** (`marceline/public_surface.py`): the same allowlist as middleware, tested
  over every routed URL, so a proxy mistake alone does not expose anything.
- **Production settings refuse to start unsafe** (`core/deploy.py`): real secret key and database
  password, explicit hosts, https-only CSRF origins on those hosts, secure cookies, HTTPS redirect,
  HSTS, `LOGIN_TRUSTED_PROXIES ≥ 1`, an explicit surface. `manage.py check --deploy` passes except
  the HSTS preload warning (preload: operator said no).
- **gunicorn** (2 workers × 2 threads, unprivileged, no ffmpeg, no tests or fixtures in the image,
  no published port); migrations are a deploy step, never run on start. **Postgres 17** on an
  internal network (accounts, sessions, login attempts, the cache table). The SPA is one build
  (ADR 0026); staff screens ship in it but are unreachable. Images are built on development and
  loaded over SSH (`docker save | ssh … docker load`) — production needs no registry, no GitHub
  credentials, no build memory.
- **Sentry** (`core/sentry.py`): EU data region enforced (`*.de.sentry.io`), no PII, no local
  variables, no request bodies, cookies, headers or query strings, no breadcrumb payloads, no
  tracing. `sentry-sdk` is a dependency (2026-09-17); reporting is on only when `SENTRY_DSN` is set.
- **Health**: container health checks; a free external uptime monitor polls
  `https://dash.marceline.studio/api/v1/health` (runbook). A `cache` block in the health answer (oldest bundle
  age, last fetch failure) is part of building the cache.
- **Backups**: production holds nothing that development cannot recreate — accounts are copies,
  bundles are caches, sessions are disposable — so no off-host backup of production in phase 1, and
  production never receives Storage Box credentials (one SFTP account holds the development backups).

### Transitional state

Until the production VPS exists, the stack runs on the development VPS as the separate compose
project `marceline-prod`, bound to `127.0.0.1` with `APP_DOMAIN=localhost`, fetching from the
export listener over loopback. It is **never** exposed publicly from the crawler host. Moving is:
provision, join the tailnet, copy `infra/prod/` and `.env`, load images, migrate, set DNS, point
the export URL and purge target at the new nodes.

## Costs of this model compared with the snapshot push

Honestly, the read-through cache is simpler to operate and closer to what the operator wants, but
it gives up two properties the push had:

- **Development now runs a listener production can reach.** In the push model development listened
  to nothing new. Mitigations: Tailscale only (never public), an ACL admitting only the production
  node to one port, a separate process whose URL allowlist is the export and nothing else, a scoped
  and rotatable token, a `SELECT`-only database role, per-token rate limits, and bundles only for
  organisations that exist — no query language, no "export everything".
- **A compromised production can ask development for any organisation's customer data** (it holds
  the token and knows the ids). That is the same set of data a snapshot copy on production would
  have held anyway — every organisation's customer-visible documents — and no more: no frame
  hashes, no crawl portfolio, no safety records, no staff data, no path into the admin. Rotating
  the token cuts it off.
- **A cold cache needs development.** After a rebuild of production, or for an organisation nobody
  opened recently, the first request fails (503) while development is down. The push model always
  had a full copy. Accepted: development outages are expected to be rare and short, and a warm-up
  job can be added if they are not.
- In exchange: no SSH automation keys, no spool directory or systemd units, no schema-fingerprint
  dance between two databases, no row allowlist to maintain, and freshness driven by use.

## Alternatives

- **A. Signed snapshot pushed over SSH (runner-up; the first draft).** Development renders the
  cache contents and pushes them with `ssh publish@prod < bundle`, where the key's
  `authorized_keys` entry forces a single receive command. SSH was the transport there for two
  reasons: it gives **strong transport authentication with a forced command** (the key can do
  exactly one thing), and it needs **no listening service on development** — every connection
  starts on development, and production holds no credential or network path back. It loses to the
  read-through cache on moving parts (spool, apply unit, schema fingerprint, a full row allowlist),
  and on freshness, which is interval-driven instead of use-driven. It becomes the better choice
  again if development must not accept any inbound connection at all.
- **B. Postgres logical replication.** Production would hold a replication role into the main
  database, row filters cannot express ownership or visibility joins, DDL is not replicated, and a
  down subscriber retains WAL on the main database until its disk fills.
- **C. A row-level cache on production** (the same tables, filled lazily). Needs the customer read
  model and its tenancy and safety rules to run on production against partial data — a second
  implementation of the most sensitive code we have.
- **Rendering responses on development and pushing static files.** Loses invalidation and per-use
  freshness, and is otherwise model A.

## Consequences

- Customers see data at most 15 minutes old, 72 hours when development is down (labelled as such),
  and a flagged page disappears from production the moment the purge arrives.
- Development can be down, rebuilt, migrated or moved to Hetzner without the customer app going
  down for organisations that were used recently.
- Built in phase 1: the export listener and its token; the envelope signing and verification; the
  cache tables, TTL/stale logic and the per-organisation lock; the purge endpoint, its
  Tailscale-only Caddy site and the development purge outbox; the accounts pull; `X-Data-As-Of`.
  Still to build: the export's `SELECT`-only role and rate limit, the "last updated" line in the
  customer shell, the health `cache` block, and a scheduler job for `sync_cases`.
- Ed25519 comes from `cryptography` (added 2026-09-17).
- ADR 0005 is amended: the public VPS is the small cache; the internal VPS holds the main database
  and the crawlers.

## Operator decisions

Taken 2026-09-17: read-through cache instead of SSH push, signature kept; freshness 15 minutes and
instant invalidation on safety; Sentry in the EU region; a free external uptime check; Hetzner Cloud
CX22 class for production; development to Hetzner after the Netcup contract year; no HSTS preload.
Later the same day: this revision accepted; `cryptography` and `sentry-sdk` added; the customer app
hostname is **`dash.marceline.studio`**; Next.js with a private Django is phase 2, not now.

Still open (operator actions, not decisions): the DNS records for `dash.marceline.studio`, the
Sentry EU project, generating the signing key and the export token on the hosts (runbook).

## Implementation, phase 1 (2026-09-17)

Built as decided above, with these concrete choices:

- **Roles and surfaces.** `MARCELINE_ROLE=dev|prod` picks the machine (default follows
  `DJANGO_ENV`). Surfaces: `full` and `export` (dev), `customer` and `internal` (prod); a surface of
  the other role refuses to start (`core/deploy.role_problems`). Besides the per-surface allowlist,
  `marceline/public_surface.py` answers 404 for `/internal/v1/export/…` on prod and for
  `/internal/v1/cache/…` on dev, whatever the surface. The internal API is a separate `NinjaAPI`
  (`marceline/internal_api.py`) with no docs and no OpenAPI document.
- **`delivery` app** (depends on `core` only): Ed25519 envelopes (`envelope.py`: JSON
  `{alg, payload, signature}`, signature over a domain prefix plus the payload bytes, `format` and
  `schema_version` inside), SHA-256-digested service tokens (`tokens.py`), the cache (`store.py`),
  the purge receiver (`purge.py`), HTTP between the machines (`client.py`), and the tables
  `ServiceToken`, `CachedBundle` + `CachedDocument`, `CacheTombstone`, `PurgeNonce`,
  `ReplicatedAccounts`, `PurgeOutbox`.
- **The bundle is rendered by the customer views themselves** (`marceline/export.py`): each path
  is resolved through the URLconf and the view is called for a member of the organisation, so
  status, content type and body are byte-identical to development's answer (tested). Documents:
  `originals`, `findings`, `cases`, `removals`, `originals/{id}` and `cases/{id}` per owned original,
  and `originals/*` / `cases/*` — the exact 404 for an id that is not in the bundle. An
  organisation without an active member has no bundle (404 from the export). Since ADR 0033 the
  `{id}` in a key is the original's public id (`orig_…`); purges and `page_ids` stay primary keys.
- **Production serves customer data only from the cache** (`marceline/customer_cache.py`, the
  innermost middleware on `prod`): matched by URL pattern after resolving, with `X-Data-As-Of`
  (ISO 8601 UTC, the signed render time). `me` and `auth/*` stay local. 503
  `data_temporarily_unavailable` with `Retry-After: 60` when nothing is servable.
- **Freshness**: TTL 15 min and maximum staleness 72 h are constants in `delivery/store.py`;
  `expires_at` = render time + 72 h is signed; development's clock may run 5 min ahead. A stored
  bundle never goes backwards in `as_of`.
- **Single flight**: a session-level advisory lock per organisation; with a stale bundle the other
  requests answer stale at once, without one they wait up to 5 s. Stores and purges are serialised
  by a transaction-level advisory lock, and a `delete` purge tombstones its pages, so a fetch that
  raced a safety purge cannot store the page again.
- **Purges** (`marceline/cache_purge.py`): a page leaving safety-clear (ORM saves, via `Page`
  signals in `marceline/apps.py`) → `delete` of that page; confirm/reject/reset in the review API →
  `expire` of the owning organisations (resolved through `ownership.owned_references` only). Written
  to `PurgeOutbox` in the causing transaction, sent after commit with 3 quick attempts, retried by
  `send_cache_purges`; bulk `.update()` flags (`safety_scan`) are caught by its
  `--flagged-within-minutes` sweep. Purges carry `issued_at` (±5 min) and a single-use nonce.
- **Accounts**: `GET /internal/v1/export/accounts` (organisations; users with a membership:
  username, password hash, active flag, role; never staff flags) and `manage.py pull_accounts` on
  production (cron, every 15 minutes), which replaces production's users/organisations/memberships
  by development's ids. The coverage bucket (`monitored_sources`) travels with it.

**Seams left for later** (each documented where it lives):

- the export listener's **`SELECT`-only database role** and **per-token rate limit** are not built;
  the export writes nothing (`CASE_SYNC_ON_READ=false` on the listener, tokens are only read);
- **`sync_cases` is not scheduled** — until it is, the export reads cases as the last manual sync
  left them (the scheduler is owned elsewhere);
- the **health `cache` block** (oldest bundle age, last failure) is not in `/api/v1/health` yet;
  `CachedBundle.last_failure` / `last_failure_at` hold the data;
- the **"last updated" line in the customer shell** (`frontend/src/app/`) is not built; the header
  is there;
- crawler and media worker processes do not send purges themselves (their compose services are
  owned elsewhere); the sweep covers them within its cron interval, and the TTL bounds it;
- the development web process records purges (`CACHE_PURGE_OUTBOX=true`) but holds no key; the
  `export` service delivers them from cron, so a purge from a review decision reaches production
  within that cron interval rather than immediately;
- `Reference` ownership becoming a column changes nothing here: everything goes through
  `marceline/app/ownership.py`.

## Amendment 2026-09-17: customer uploads stay on development until the outbox exists (issue #29)

The first customer write that is not a session is `POST /api/v1/app/fingerprints` (add an original
from browser fingerprints, ADR 0019 amendment). The decision table above already says where such
writes belong: a production **outbox** of validated events that development pulls. It is not built,
and a write on production today would land in production's own tables, which hold no originals and
are never read back by development. So, until the outbox exists:

- **Development serves the endpoint** (reached over Tailscale, like the rest of the full surface).
- **Production refuses it**: with `MARCELINE_ROLE=prod` the endpoint answers **503
  `adding_originals_unavailable`** before reading anything, and client events do the same; nothing is
  written or counted. The path stays inside the customer allowlist (`/api/v1/app/`), so no surface
  change is needed now or later.
- **The app does not offer it there**: `GET /api/v1/app/me` carries `can_add_originals`, true only for
  a member of an organisation on a machine that takes uploads. Production answers `me` from its local
  accounts, so the flag is false there by construction, and the overview shows no "Add an original".

When the outbox is built: production validates with the same `validated()` and rate limit, appends the
document and the rights declaration as one event, and development imports it through the same
function; the answer becomes "received, being added" (202) rather than the original. Two things to
change then: Caddy's `request_body max_size 1MB` for `/api/v1/app/*` needs a 16 MB exception for this
one path (a three-hour original is about 12.5 MB of JSON), and the production cache purge for the
organisation (`expire`) must follow the import so the new original appears.

## Amendment 2026-09-17: the production write path (customer writes through an outbox)

Operator decision, 2026-09-17: build the write path, so customers can add originals on
`dash.marceline.studio`. This replaces the 503 of the amendment above wherever the outbox is
switched on (`OUTBOX_ENABLED=true`); without it production still answers 503
`adding_originals_unavailable` and `me.can_add_originals` stays false.

### The flow

```
browser ──POST /api/v1/app/fingerprints──▶ prod web: session, CSRF, rate limit, validated()
                                             │ store delivery.OutboxItem (payload = request JSON)
                                             ▼
                                           202 {submission: {id: sub_…, status: processing}}

dev export container (cron, every minute): pull_customer_writes
   POST prod-internal:8081 /internal/v1/outbox/pull   (signed by DEV key: issued_at, nonce)
   ◀── batch of pending items, oldest first           (signed by PROD key, echoes the nonce)
   for each item, in order: apply through the development endpoint's own function,
       in one transaction with delivery.InboxItem (idempotent by item id)
   POST prod-internal:8081 /internal/v1/outbox/ack    (signed by DEV key: outcomes)
                                             │ prod: status + code, payload emptied,
                                             ▼ the organisation's CachedBundle expired
browser ──GET /app/fingerprints/submissions (prod-local) and /app/originals (refetched bundle)
```

### Who signs what

| Message | Direction | Signed with | Verified with | Replay protection |
|---|---|---|---|---|
| bundle, accounts, purge (unchanged) | dev → prod | dev private key (`CACHE_SIGNING_KEY_PATH`) | dev public key on prod (`CACHE_VERIFY_KEY_PATH`) | as before |
| `marceline.outbox-pull` | dev → prod | dev private key | dev public key on prod | ±5 min, single-use nonce (`PurgeNonce`) |
| `marceline.outbox-batch` (the answer) | prod → dev | **production's own** private key (`OUTBOX_SIGNING_KEY_PATH`, `internal` container only) | production's public key on dev (`OUTBOX_VERIFY_KEY_PATH`) | must echo the pull's nonce, ±5 min |
| `marceline.outbox-ack` | dev → prod | dev private key | dev public key on prod | ±5 min, single-use nonce |

Each side signs what it originates. Customer writes originate on production, so production signs
them; development never holds production's private key and production never holds development's.
The public `web` process holds **no private key at all**: it only accepts writes into its database;
the Tailscale-only `internal` process signs batches. `make prod-config` refuses the outbox key on
any other service.

What the signatures add on top of Tailscale: a node that is not development cannot read the outbox
(customer titles and hashes) or settle items; a node that is not production cannot inject writes
into development (which would create originals owned by any organisation); an old batch cannot be
replayed into a later pull. What they cannot do: a **fully compromised production** can invent
writes — it holds every customer session, so it could submit them through the API anyway. The
blast radius stays what a signed-in customer can do: development re-validates each write, never
lets a write claim another organisation's file (`claim_needs_review`), and records who declared
the rights.

### Checks on both sides

- **Production, before accepting:** authentication, CSRF (unchanged middleware), membership, the
  per-organisation rate limit (`upload_throttle`, counted on production), and `validated()` — the
  same function development runs. A double submit of the same file while the first is pending is
  answered with the existing submission. Production cannot decide what needs development's data
  (already owned → 200, another organisation's file → 409): those outcomes come back as the
  submission's result.
- **Development, before applying:** the item's user must still be an active member of the item's
  organisation (`no_organisation` otherwise); the payload is parsed with the endpoint's schema and
  `register_original` runs `validated()` again against today's code and wording (a declaration
  version retired in between is refused `rights_confirmation_required`). Development's rate limit
  is not applied again. Client events are applied with `record_client_event`.

### Once, in order, and poison

- **Idempotent by item id.** `InboxItem` is written in the transaction that applied the item; a
  re-pulled item with a settled row is only acknowledged again. A lost acknowledgement is re-sent
  by the next run (every settled, unacknowledged row).
- **Ordered per organisation.** Batches carry pending items in acceptance order. A write that
  fails (the handler raises) stays pending and holds back that organisation's later writes for
  the rest of the run; other organisations go on. This matters for the rights writes to come
  (declare, then withdraw).
- **Poison.** After `MAX_APPLY_ATTEMPTS` (3) failures the item is **quarantined**: settled with
  reason = the exception's class name (never its message, which may quote the payload), payload
  kept on development only, acknowledged as `quarantined` so it no longer blocks anyone. The
  customer sees "not added yet — we are looking into it"; `pull_customer_writes --retry sub_…`
  applies it again after a fix, and production accepts `quarantined → done|refused`. The ERROR log
  line (Sentry) names the item and the class only. An unknown kind (production newer than
  development) is a failure too: deploy development first.

### Results reach the customer

- The acknowledgement settles the item (`done` + `created`/`already_present` + the original's
  public id, or `refused` + the endpoint's code) and **expires the organisation's cached bundle in
  the same transaction**; development also records the usual `expire` purge. A bundle whose render
  time is older than an applied write (a fetch that raced the apply) is stored already expired, so
  the next request refetches.
- `GET /api/v1/app/fingerprints/submissions` is a **production-local** read (not in the bundle,
  like `me`): the organisation's uploads with `status: processing | done | refused | failed` and a
  code. On development it is empty. The app shows "being added" (DE "wird hinzugefügt") on the
  add page after a 202 and on the overview while anything is processing (polling every 15 s),
  refetches the originals when one settles, and words refusals with the upload errors' sentences.

### Staleness, failure, retention

- **Stale writes:** `outbox_housekeeping` (production cron, every 15 min) logs an ERROR (a Sentry
  event) once per write pending longer than `OUTBOX_STALE_ALERT_HOURS` (**0.5 h / 30 min**,
  operator decision 2026-09-17; a pull runs every minute).
- **Paused:** when development has not pulled for `OUTBOX_PAUSE_AFTER_HOURS` (**6 h**, operator
  decision 2026-09-17), or never has, production refuses new writes with **503
  `processing_paused`** and `Retry-After: 3600`, before counting them; the app says adding is
  paused and nothing was saved. Housekeeping logs an ERROR on every run while paused. Rationale:
  the outbox exists to ride out short development downtime, but a customer should not watch an
  upload wait for days — and a half-hour alert plus a six-hour pause catch a stuck puller fast
  without flapping on an ordinary short cron gap.
- **Retention:** the payload (hashes and metadata only) is emptied the moment an item settles;
  settled rows go after **7 days** (they carry title and outcome for the customer). Development
  deletes acknowledged, applied or refused `InboxItem`s after 7 days; quarantined rows keep their
  payload until retried.
- **Backups:** pending writes live only on production until pulled (minutes). A lost production
  database loses at most the writes of that window, and the customer's submission list with it.
  The optional local nightly dump (runbook section 8) now also saves those.

### Scope: every customer write under `/api/v1/app` (audited 2026-09-17)

| Endpoint | On production |
|---|---|
| `POST /app/auth/login`, `/logout` | local, as before (sessions are production's own) |
| `POST /app/fingerprints` | outbox, kind `add_original` (202) |
| `POST /app/fingerprints/client-events` | outbox, kind `client_event` (200 `recorded`) |
| rights API (ADR 0034: claimant, self-declaration, withdrawal, licensed partners) | outbox, one kind per write (`rights_claimant_type`, `rights_account_token`, `rights_declare`, `rights_withdraw`, `rights_declare_authority`, `rights_withdraw_authority`, `rights_add_partner`, `rights_remove_partner`, `rights_affirm_no_partners`); each handler in `marceline/customer_writes.py` calls the same `marceline.rights.services` function the development endpoint uses. Reads (`GET /app/rights`) come from the cache bundle, added to the export the same way as originals/cases. |
| rights proof documents (vault) | **development only, deliberately**: sensitive identity and contract documents must never be stored on the public host, not even in transit through an outbox. `POST /app/rights/proofs` stays under `ROLE_ONLY_PREFIXES["dev"]` and answers 404 on production. |

### Edge

- **Caddy:** `request_body max_size 16MiB` for exactly `POST /api/v1/app/fingerprints` (a named
  matcher placed before `@customer_api`; Caddy tries named matchers in file order); every other
  customer route keeps 1 MB, the client-events path included. Django's own cap
  (`fingerprints.MAX_BODY_BYTES`, 16 MiB) is the second wall. The `:8081` Tailscale site forwards
  `POST /internal/v1/outbox/pull` and `/ack` (256 KB requests) besides the purge.
- **Django:** the `internal` surface allows exactly the purge and the two outbox paths;
  `/internal/v1/outbox/` is production-only (`ROLE_ONLY_PREFIXES["prod"]`) and 404 unless
  `OUTBOX_ENABLED`. The customer surface is unchanged: the new read lives under `/api/v1/app/`.
- **Sizes:** a batch carries up to 8 MiB of payload (always at least one item, so a 16 MiB
  original travels alone, about 22 MB on the wire, below `MAX_ENVELOPE_BYTES`); the `internal`
  container gets 384 MB (was 256 MB).
- Tests: `delivery/tests/test_outbox.py`, `test_inbox.py` (signatures both ways, ordering,
  idempotency, quarantine, retention), `marceline/tests/test_customer_writes.py` (both roles in
  one database over the HTTP listener), `test_prod_edge_config.py` (the Caddyfile's numbers and
  paths without docker); `make prod-surface-check` probes the limits against the real Caddy.

### Seams left

- The pull runs in the `export` container, which already writes (`send_cache_purges`). When the
  export gets its `SELECT`-only role, both cron commands move to a process with the normal role.
- One puller at a time (advisory lock). Customers cannot dismiss a refused submission; it
  disappears after 7 days.
- Rights writes (above) wait for ADR 0034 on main.

### Operator decisions (2026-09-17)

- Thresholds decided: `OUTBOX_STALE_ALERT_HOURS=0.5` (30 min alert), `OUTBOX_PAUSE_AFTER_HOURS=6`
  (6 h pause) — tighter than the 2 h / 24 h proposal above, so a stuck puller is caught well within
  a working day.
- Rights writes (ADR 0034) are wired: `marceline/customer_writes.py` gained kinds and handlers for
  self-declaration, withdrawal, claimant type/profile edits and licensed-partner edits, all going
  through the same `marceline.rights.services` functions the development endpoint calls; proof
  document uploads stay development-only (the vault never travels through the outbox). The rights
  prefix left `ROLE_ONLY_PREFIXES["dev"]` except `/api/v1/app/rights/proofs`, and rights reads
  (the overview) were added to the export bundle following the delivery/export pattern (see
  "Scope" above, updated).
- The runbook's setup steps (production's outbox key, `OUTBOX_ENABLED`, the cron lines) remain
  operator actions, unchanged by this decision.
