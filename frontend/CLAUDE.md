# Frontend conventions

Internal moderation SPA (ADR 0014, on ADR 0004): Vite + React 19 + TypeScript strict, Tailwind v4,
TanStack Router (code-based routes in `src/router.tsx`) and TanStack Query. Node >= 20.19, npm.

What must not break here, and the single place each rule is answered. The screen-by-screen detail —
the layout tree, the confidence and availability display, the keyboard map, the parity measurements
and the narrative behind each rule — moved verbatim to `docs/conventions/frontend.md` on 2026-09-18.
The root `CLAUDE.md` hard rules apply here too.

**Two customer apps, one of them on its way out.** The customer UI under `/app` (ADR 0026) exists
twice while phase 2 lands: in this SPA, and as a Next.js project in `frontend/customer/`
(ADR 0036), which is what production serves with `CUSTOMER_APP=next`. **They share one copy of the
words and of the pure logic** — `src/i18n/{en,de,language}.ts` (+ the slices in `src/i18n/{en,de}/`)
and several `src/app/*.ts` modules —
imported there through `@shared/*`, so a change to either reaches both. `lib/routes.test.ts` in the
Next project fails when this SPA gains an `/app` route the Next app does not have: **add the screen
in both, or the migration silently loses it.**

**The customer app addresses the creator with „du"** (operator decision 2026-09-19), lower-case,
like the public website — every German string in `src/i18n/de/*.ts` (all eight slices, including
`onboarding`, `verification` and `accountEmail`) is checked by `src/i18n/address.test.ts`, which
fails on a formal `Sie`/`Ihr…`/`Ihnen`; the staff moderation SPA has no German resource and is
unaffected. The two
customer **mails** (ADR 0054) follow the same rule and are checked in
`backend/accounts/tests/test_account_mail.py`, because their text lives in the backend.

**The customer words are stored per screen** (issue #99, ADR 0036 amendment 2026-09-18).
`src/i18n/{en,de}/core.ts` is what every `/app` visit pays for — the shell, the sign-in form, the
overview and the small screens — and `caseFile`, `addOriginal`, `rights` and `notices` are a slice
each, loaded by that screen's own route (`src/i18n/slices.ts` names which, `router.tsx` loads by that
map); `en.ts` / `de.ts` compose every slice for the tests and for the Next app, which renders on the
server and ships no resource at all. **A new word goes in the slice of the screen that shows it** —
in `core.ts` every visitor pays for it and `CORE_WORDS_BUDGET_KB` (4.5 KB) fails. A key the shell or
the overview also names belongs in `core.ts` even when its section is a screen's: three sections are
split that way and `en.ts` merges them back, so **no key path depends on which module a key lives
in**. Two guards: `i18n.test.ts` asserts every key is in exactly one slice, and
`src/app/translationKeys.test.ts` that no screen names a key from a slice its route does not load —
the one way this design can render a key to a paying customer. It took the SPA's `/app` first load
from 149.88 KB gzip to 142.65 and **ended ADR 0044's `src/i18n/upload.ts` exception** (those words
are part of the `addOriginal` slice). Full passage and the superseded rule:
`docs/conventions/frontend.md`.

## Customer app conventions live next to the code
**Before changing the customer screens, read the `CLAUDE.md` of the directory you are in** — a tool
that reads files through the shell does not load it by itself, so open it deliberately.
- `frontend/src/app/CLAUDE.md` — Customer UI in the SPA, and the customer **wording rules**
- `frontend/customer/CLAUDE.md` — The Next.js customer app

## Commands
`make frontend-install` / `-dev` (Vite on `${WEB_BIND:-127.0.0.1}:5173`, `/api` proxied to Django
on 8000) / `-build` / `-api` / `-check` (typecheck, eslint, vitest, API type drift, `/app`
bundle-size budget; part of `make check`). Single test:
`cd frontend && npx vitest run src/lib/keyboard.test.ts`.

## Privacy rule (hard)
- **Default is privacy mode: no image requests at all.** Load mode is an explicit toggle stored in
  `localStorage` (`marceline.imageMode`); unreadable storage means privacy.
- Images render only through `MediaGrid`, only from `mediaImageUrl(id)` =
  `/api/v1/media/{id}/image` (server-side passthrough). Never put a page or media URL into `src`,
  `srcset`, CSS `url()` or `<video>`. ESLint rejects absolute `src` literals; the build's CSP
  (`img-src 'self'`) blocks the rest.
- Pages with `safety_status` review/blocked: the API sends no media ids (`media_hidden`), and
  `MediaGrid` renders no `<img>` in any mode.
- **Every image lazy-loads** (operator, 2026-09-18): `loading="lazy"`, `decoding="async"` and a box
  the parent sizes, so a queue of a hundred thumbnails fetches only what the reviewer scrolls to and
  nothing reflows. `MediaGrid` already does this; any new image surface must too. It is a privacy
  rule as much as a performance one — each image is a real request through our passthrough, and one
  the media's host can see us make.
- Safety screen is text only: no media components, and page URLs are plain text, not links. Its
  *Safety incidents* section shows the server-rendered hotline draft verbatim in a read-only
  textarea — **never rebuild the text in the browser**.
- **No external resources for source candidates** either: evidence (queries, result URLs, linking
  pages, redirects) is text, never `href`/`src`; only the candidate domain itself is a plain link.
- Page links elsewhere: `target="_blank" rel="noopener noreferrer"`, http(s) only (`safeHref`).
- **Never fetch a page from the browser.** "Check availability now" only queues crawl tasks.
- No external fonts, CDNs or analytics. `<meta name="referrer" content="no-referrer">`.

## API client and queries
- Change the backend API → `make openapi` → `make frontend-api`. Both files are committed and
  drift-checked. **`src/api/schema.d.ts` is generated — never edit it by hand**, and never
  hand-write request/response types; alias them from `Schemas[...]`.
- Call endpoints as `unwrap(api.GET("/api/v1/...", { params, body }))`; it throws `ApiError`.
  Auth is the Django session cookie: `GET /auth/me` sets `csrftoken`, the client middleware sends it
  as `X-CSRFToken` on unsafe methods, and a 401 anywhere refetches `me` and shows the login screen.
- **Same origin only: relative `/api/...` URLs.** No absolute URLs, no third-party hosts.
- `src/api/errors.ts` and `src/api/queryKeys.ts` deliberately import no openapi-fetch client — the
  `/app` bundle budget (ADR 0028) depends on it. That script guards four numbers, not one: the
  `/app` first load (150 KB), the **framework entry** on its own (`ENTRY_BASELINE_KB`, 107 KB — React,
  ReactDOM, TanStack Router/Query), the core words (`CORE_WORDS_BUDGET_KB`, 4.5 KB) and each
  additional route (`ROUTE_BUDGET_KB`, **16 KB** — operator decision 2026-09-18, down from ADR 0028's
  30, because the per-route number is the only one that measures our own code and no route of ours
  was ever near 30). So a regression names its own cause instead of showing up as "the total grew";
  it also fails if anything shipped imports the composed `en.ts` / `de.ts` statically, which would
  undo the per-screen split.
- Queues use `useInfiniteQuery` with the server's keyset cursor, `staleTime: Infinity` and
  `gcTime: 0`. Decisions are optimistic (`decided` → `settled` with the ids the server actually
  changed, or `failed`, which reverts). Invalidate `counts` after writes, not the queue; queue
  screens remount (React `key`) when their filter changes.
- Order and labels come from the server: **do not recompute confidence weights in the browser**, and
  a null `timeline_score` is neutral — never style it as a penalty.

## PDQ hashing (fingerprinting client, issue #29 / ADR 0017)
- `lib/pdq.ts` is the only entry point (`hashDihedral`, `hashDihedralLuma`, `toHex`/`fromHex`/
  `hammingDistance`): lazy WASM init, no React, no DOM — a Web Worker imports it as is.
  `src/wasm/pdq-wasm.js` is **generated** by `frontend/wasm/pdq/build.sh` (pinned emsdk image,
  Docker only, output committed) — never edit it; rebuild instructions and why `-ffp-contract=off`
  is mandatory are in `frontend/wasm/pdq/README.md`.
- `lib/fingerprintClient.ts` decodes on the page and hands the Worker both the frame hashing and the
  file's SHA-256; failures carry a `reason` code and `maxPositions` refuses an over-long video
  before hashing. **Load it only with `import()`** (staff and customer screens both do).
- **Parity is the contract.** Fixtures live once, in `backend/matching/tests/fixtures/pdq/*.json`;
  `src/lib/pdq.test.ts` requires identical hex for all 8 orientations and identical quality, and
  `matching/tests/test_pdq_parity.py` catches drift on the Python side. **Never weaken either
  assertion** — hashes that differ silently look like a matching failure, not a build failure.
- The decode path is verified in a real browser, not emulated (`frontend/parity`, issue #28):
  `make browser-parity` is **not** part of `make check` (browser download, ffmpeg, a minute of
  decoding). Run it after changing `fingerprintClient.ts`, `videoFingerprint.ts` or the WASM hasher.
  Measurements and why the browser tag stays separate: `docs/adr/0022`, `docs/adr/0019`,
  `frontend/parity/README.md`.
- **How long it takes is measured too, and there are no intensity levels** (issue #29,
  `docs/research/browser-fingerprinting-speed.md`). `make browser-speed` reports seconds of wall
  time per minute of video, per resolution and per CPU budget. **The seek dominates, not the PDQ
  hash**, and the pure-JS SHA-256 is a floor no sampling rate lowers — so "hash fewer moments" buys
  little and costs recall on poster-only pages, which are 64 % of the index. `DEFAULT_FPS` stays 1,
  the screen shows an estimate instead (`estimateRemainingMs` in `lib/fingerprintProgress.ts`), and
  **a fast/medium/thorough picker is a decision that was made against** — reopening it needs the
  numbers in that document, not a fresh opinion.
- **A photo set is hashed entirely in the Worker** (issue #116, ADR 0053): `fingerprintPhotos`
  hands it one file at a time and the Worker digests, decodes (`createImageBitmap` +
  `OffscreenCanvas`, `imageOrientation: "none"`, `colorSpaceConversion: "none"`) and hashes it, so
  memory is bounded by one picture and **no object URL and no `<img>` exists anywhere** — the
  customer's own photographs are not an exception to issue #53. `lib/photoFingerprint.ts` is the
  pure half: the collection digest is `matching.fingerprint.collection_sha256` byte for byte, and
  `frame_index` is the image's ordinal **in sorted-digest order** (the CLI sorts by path; a browser
  has none and must send none). A per-file failure is counted, never fatal. Its own pipeline tag
  `browser-rgb-pdq-v0`: the decoder is not Pillow's and the gap is unmeasured —
  `make browser-parity PARITY_ARGS="--photos"` measures it, on demand.
- **`seeked` is not "the picture is on the canvas"** (issue #29, ADR 0022 amendment 2026-09-18).
  Two runs of the harness each showed one sampled position in sixty hashed from the **previous
  second** — past every threshold — because `drawImage` drew what the compositor still had.
  `seekTo` waits for `requestVideoFrameCallback`, armed *before* the seek, skipping a frame more
  than 0.25 s early and capped at 250 ms after `seeked` so a seek that presents nothing cannot
  hang. **Do not "simplify" it back to awaiting `seeked`** — three clean runs are what the parity
  numbers rest on.

## Design system (`src/index.css`)
- **Tokens only.** `src/index.css` carries the customer app's token set — same ramps, same type
  scale, both themes — so a staff screen and a customer screen are one product. `src/tokens.test.ts`
  fails on a raw Tailwind palette class anywhere under `src/` except `src/app/**`, and on any
  `url()` in the stylesheet: **no font files here** (this app has no `public/`, its `/fonts/…` would
  404 behind the Caddy that serves the customer rollback from the same build).
- **`blur-thumb` is ours.** The customer app forbids it (it shows no media); this app is the surface
  the rule was written for. Privacy mode still makes no request and a safety-flagged page still
  renders no `<img>` in any mode.
- **Radius carries meaning**: evidence is square (`rounded-none`), chrome softens (`sm` controls,
  `md` cards, `lg` dialogs), `rounded-full` only for pills, meters and avatars.
- **Automation is violet** (`accent`): a PDQ match, a cluster proposal, a `decided_by=auto` row, a
  derived copy extent, a job's own note. A human decision is neutral or red. Tones are named for
  meaning in `lib/tones.ts` and `lib/confidence.ts`, **never for a hue**.
- **Spend the red.** Brand red is the active nav marker, the one primary action, a rejection and a
  live problem. **Colour never works alone: every badge also says its state in words.**
- The focus ring is stated once in `index.css` for every focusable element; no component sets its own.
- Staff wording is not customer wording: leak, case, confidence, band and distance are correct here.
  `src/i18n/wording.test.ts` governs the **customer** strings only (the rules themselves are in
  `frontend/src/app/CLAUDE.md`).
- Provenance is **derived** by the API (no decider is stored); say so in UI copy, and the status
  legend states that nothing is auto-confirmed.
- **Phone first for the customer app** (issue #117, operator decision 2026-09-18): every new
  customer screen is designed at 360 px first, a list declares its stacked form, no affordance is
  hover-only, inputs are ≥ 16 px and controls ≥ 44 px. The rules and the guards are in
  `frontend/customer/CLAUDE.md`; the full passage is `docs/conventions/customer-ui.md` "Phone
  first". `src/index.css` carries the **CSS half only** — the 16 px / 44 px floors under
  `@media (pointer: coarse)` — and the SPA's `/app` copy is otherwise left as it is: it is ADR
  0028's rollback, not a screen to re-lay-out twice. The staff SPA is a desktop surface over
  Tailscale, so that query almost never matches here.

## Style
- TypeScript strict + `noUncheckedIndexedAccess`; no `any`, no non-null assertions (lint).
- Tailwind utility classes on the tokens above; small shared primitives in `components/ui.tsx`.
  Accessible names on controls, `role="switch"` for toggles, `role="dialog"` + `aria-modal` for
  dialogs.
- Tests: vitest. Pure logic in `src/lib/*.test.ts` (node); components with
  `// @vitest-environment jsdom` and Testing Library. **No network in tests.**
- Numbers and small bars only on the overview — no chart libraries, CDNs or fonts.
