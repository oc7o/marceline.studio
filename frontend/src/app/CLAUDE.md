# Customer UI in the SPA (`/app`)

What must not break here, and the single place each rule is answered. The screen-by-screen detail —
the route/reads table, what each screen renders, the bundle numbers and the narrative behind each
rule — moved verbatim to `docs/conventions/customer-ui.md` on 2026-09-18. The root `CLAUDE.md` hard
rules and `frontend/CLAUDE.md` apply here too; the Next.js app that will replace these screens has
its own `frontend/customer/CLAUDE.md` and shares this directory's words and pure logic.

## The shell (ADR 0026)
- **In production only `/app`, `/app/*` and `/assets/*` are served** (`infra/prod/Caddyfile`,
  ADR 0028); `/` redirects to `/app`. Every staff API answers 404 there, so **the customer UI must
  never link to or call anything outside `/app` and `/api/v1/app/…`** — it would work in development
  and break in production. **Never put data or secrets in staff screen code**: it still ships to the
  internet, on demand, for anyone who requests its chunk URL directly.
- **Separate audience, separate shell.** `RootShell` (`src/router.tsx`) picks the layout from the
  matched route, so an `/app` URL never renders the staff layout and no `GET /auth/me` is attempted.
- **Performance budget (ADR 0028).** Every route in `src/router.tsx` — staff and customer,
  including `RootLayout` and `CustomerApp` themselves — is a lazy route component, so a customer
  never fetches the staff screens and a staff URL never fetches the customer app or its i18n
  resources. `frontend/scripts/check-bundle-budget.mjs` (run by `make frontend-check`) fails on a
  regression — the `/app` first load (142.65 KB gzip today, budget 150), and since issue #99 the
  framework entry and the core words on their own lines, so a regression names its own cause;
  **read the script's own comments before changing any of those numbers.**
- **No pathless layout routes** (`createRoute({ id })`): TanStack types `Link`'s `to` against route
  *ids*, and a pathless parent prefixes every descendant's id. Add customer screens as children of
  `appRoute`.
- `CustomerApp` gates every `/app` route except `/app/sign-in`, so **a new screen is behind the
  session by construction**. The ids it compares against live in `src/app/routeIds.ts` and a test
  asserts they still exist in the route tree.
- Auth (ADR 0023): `GET /app/auth/session` **first** — it is the only thing that sets `csrftoken` —
  then login/logout. One message per documented status in `src/app/signInError.ts` (pure, tested):
  **401 says exactly one thing** for unknown account, wrong password and disabled account alike
  (never reveal which); 403 distinguishes "no organisation" from a stale CSRF token; 429 shows the
  wait from `Retry-After` and **never retries**.
- Tests: `src/app/customerApp.test.tsx` renders the real route tree over a memory history. The
  `vi.hoisted` block there is load-bearing — openapi-fetch captures `globalThis.fetch` at import
  time, and Node's `Request` refuses the relative URLs the SPA uses on purpose. Screen tests share
  fixtures and a stubbed API from `src/app/testApi.tsx`, but each file keeps its own hoisted block.
- **"Download my data"** (ADR 0045 amendment 2026-09-18) is a plain same-origin `<a download>` to
  `GET /api/v1/app/export` in the header next to sign-out, shown only when `me.can_export_data` is
  true (never on the public host, where the route is development-only). No client code beyond the
  conditional: the browser fetches the file with the session cookie, exactly like the invoice PDF.

## Privacy (hard, issue #53)
The customer UI shows no copy of the pirated material and contains nothing *able* to load a remote
image — no `img`/`video`/`src`/`url()`/`mediaImageUrl`, no absolute URLs. Enforced twice: the eslint
block for `src/app/**`, and `src/app/privacy.test.ts`, which scans the source text so relaxing the
lint config does not quietly relax the ban. If a screen ever needs an image, it takes an id for our
own passthrough and must be incapable of expressing any other URL.

**Matched page addresses** are text; only the record (`CaseFileScreen`) links to one (`PageAddress`:
http(s) only, `target="_blank" rel="noreferrer noopener"`). The overview, detail and log never link out.

## Language
- **Every user-visible string comes from `src/i18n`, in German *and* English.** `de.ts` is typed
  `Translation = typeof en`, so a forgotten key fails `tsc`; `i18n.test.ts` fails on any key,
  interpolation placeholder or plural form present in one language only, and on a `t("…")` key the
  resources do not define. Language is browser-detected, switchable (the switch is visible before
  sign-in) and persisted in `localStorage`.
- **Only the active language ships, and only the words in view** (ADR 0028 budget, issue #99):
  `ensureI18nReady()` dynamically imports the detected language's **core** slice and is the `/app`
  route's loader, so no `/app` screen renders before it resolves; a screen named in
  `src/i18n/slices.ts` gets `loader: … loadScreenWords("<Screen>")` of its own, which implies
  `ensureI18nReady()`, so loader order does not matter. **A new screen's words are a slice and its
  route gets that loader** — only what the shell or the overview shows belongs in `core.ts`.
  **Never call `i18n.changeLanguage()` directly** — the instance is not initialised at import time;
  tests use `setLanguageForTest`, which loads every slice.

## What the screens may say (case-file API, ADR 0025)
Routes: `/app` (`HomeScreen`), `/app/originals/$originalId` (`OriginalScreen`),
`…/copies/$pageId` (`CaseFileScreen`), `/app/removals`, `/app/invoices` (ADR 0032), `/app/rights`
(ADR 0034), `/app/originals/$originalId/notices` (`NoticeDraftScreen`, issue #33, ADR 0035) and
`/app/originals/new` (`AddOriginalScreen`, lazy). Invoices are development-only for now, so a 404
renders "not shown here yet"; rights and, since ADR 0046, notices are served on production too.
What each screen reads and renders: `docs/conventions/customer-ui.md`.
- **A notice write on production is accepted, not applied** (202, ADR 0046). `isPendingWrite` puts a
  factual "being processed" line under the step — never a claim that it happened — and the three
  refusals production gives before accepting share one sentence (`not_saved`). A refusal that
  arrives *after* acceptance is reported by `GET /app/notices/submissions`
  (`useNoticeSubmissions`, `isUnsettled`) and rendered by `NoticeSubmissionsBlock` on
  `/app/notices` and `/app/originals/{id}/notices` (issue #86, ADR 0046 §7; full passage and the
  superseded rule: `docs/conventions/customer-ui.md`). The sandbox send's path is
  `/app/notices/sandbox-send/{id}`, verb first, because that prefix is what keeps it off the
  public host.
- **The notice draft leaves as a download, never as a send.** `NoticeDraftScreen` offers the
  approved text (copy button, `mailto:`) and three plain `<a download>` links to
  `GET /app/notices/{id}/export/{txt|eml|annex}` — text, an unsent `.eml` for the customer's own
  mail program, the evidence annex (`canExport`, `noticeExportPath`, ADR 0035 amendment
  2026-09-18). **Never a "Send" button** — Marceline transmits nothing.
- **The API sends codes, the UI owns the words.** Everything the screens derive lives in
  `src/app/caseFile.ts` (pure, tested); `caseFile.test.ts` asserts every code has words in both
  languages, because template-literal `t()` keys are invisible to `translationKeys.test.ts`.
- **Never invent.** A date is one the API sent. `unavailable` renders as "not available yet" with
  the API's reason, never as upcoming. Options without an endpoint are disabled buttons that say why
  ("Prepare notice draft", **never "Send"** — #46); an option the API says nothing about reads "not
  offered yet". **Marceline transmits nothing**: a notice is a draft the customer sends themselves.
- **No match confidence of any kind for customers** (2026-09-17): no band, percentage, evidence
  frame count or distance — only `evidence: fingerprint_match | no_fingerprint_match` plus the
  review day. Issue numbers are not shown to customers either.
- **Extent** (issue #62) is a fact about the copy, never a confidence: `full_copy | excerpt |
  not_determined`, and excerpts are findings like any other.
- **Customers never see our work in progress** (2026-09-17): the API sends confirmed copies only, so
  an unconfirmed finding is invisible — no "being checked" state, badge or count anywhere, and no
  first-seen date. Dates of our internal steps are date-only and render without a time.
- **Coverage is a bucket, never a list or a count** (operator, 2026-09-16): "we watch 10+ sources"
  from `monitored_sources`. **Never render which sites we crawl**; a finding's own site and URL are fine.
- **Hoster cooperation is a rounded, past-tense response history only** (operator, 2026-09-17, ADR
  0027 amendment): null, or `{removed_percent, usual_removal_time}` with a "rounded, so far" note.
  The API already rounds; **never compute, sharpen or colour it into a verdict, never show counts,
  and never bring back chance/speed bands or forecast words.**
- **Hosting** (ADR 0027) renders each finding's `hosting`: provider, country and the **suggested
  notice channel** for the `jurisdiction` code — **never "DSA applies"**. IP addresses and AS number
  only inside a collapsed "technical details"; RDAP source records are text, not links;
  `not_looked_up` says so and renders no empty rows, and a missing fact reads "not known yet".
- **Ids are public ids** (ADR 0033): `orig_`/`page_`/`inv_`/`org_` + 32 hex, strings, never database
  keys. Route params go through `parsePublicId(kind, value)` before any request; anything else, an
  old integer URL included, renders `NotFound` without asking.
- Query states are `Loading`, `LoadError` (no server text, a retry button) and `NotFound` — the same
  screen for a 404 and for a copy not among the original's findings, **so a safety-hidden page or
  another organisation's case looks exactly like a typo**. Customer reads never retry a 4xx.

## Add an original (issue #29; photo sets, issue #116 / ADR 0053)
- **The form asks what is being added first**, and `kind` is the only thing it branches on: a video
  (one file) or a photo set (many). The words follow — `addOriginal.photos.*` for the lead, the
  picker, the progress and the two failures whose video sentence would be nonsense about
  photographs (`problemKey`, `localFailure(error, kind)`, both pure and tested).
- **No picture of any kind, including the customer's own.** A thumbnail strip would need an element
  `privacy.test.ts` forbids, and "it is only their own file" is not a distinction the code can
  enforce once such an element exists. The screen shows a count and, when a picture could not be
  read, how many were left out and what to do — **never a file name**.
- **Extent for a photo original**: a collection has no timeline, so every photo match is
  `not_determined`, and that one explanation has a photo variant (`extentExplanationKey`). The
  other two cannot occur for a set, and every word in `core.ts` is paid for by every visitor.
- **The video never leaves the computer**, and the fingerprinting code never loads with the app: the
  route is `lazyRouteComponent` and the screen `import()`s `lib/fingerprintClient` when the customer
  presses start. `lib/lazyFingerprinting.test.ts` fails on a static import of `fingerprintClient`,
  `fingerprintWorker` or `pdq` anywhere else — use `import type`, or the pure helpers.
- Logic in `src/app/addOriginal.ts` (pure, tested); error codes map to `addOriginal.errors.*` keys,
  **never server text**. The declaration's words must equal
  `backend/marceline/app/rights_confirmation.json` for that version — `addOriginal.test.ts` reads the
  file. **New wording = a new version on both sides, never an edit.**
- A cancel is not counted as a failure; codec and other local failures are posted as client events
  with the decoder message and MIME type, **never a file name**.
- **Production accepts first, adds shortly after** (ADR 0028 write path): `POST /app/fingerprints`
  answers 202 there, the page says the original was received and is being added and links back to
  the overview — **never "open the original", there is none yet**. The overview polls only while a
  submission is processing. This is the customer's own upload, never our review work: no "being
  checked", no counts of anything else.

## Wording rules (customer UI, operator 2026-09-17)
**Marceline states technical facts; the customer makes the legal assertion.** Applies to every
customer string in `src/i18n` (both languages), customer API descriptions and customer docs.
Internal code names (`leakTimeline`, `caseFile`, `confirmed_on`, route paths) and the staff UI keep
their names.
| Never say | Say instead (EN / DE) |
|---|---|
| leak | matching copy / übereinstimmende Kopie (list item: matched page / Fund) |
| case, case file | record / Vorgang (the file: Vorgangsprotokoll) |
| confirmed, verified | human-reviewed match, reviewed by a person / manuell geprüft |
| "DSA applies", "EU host", "DMCA applies", "which law applies" | Suggested notice channel: … / Vorgeschlagener Meldekanal: … (or the plain provider country) |
| infringing, illegal copy, verified infringement, pirate | matching copy, matched page, sites like this |
| we remove leaks, on your behalf, sending it for you / by us, escalate, handled | "You send the draft from your own account" (v1 is customer-sent drafts only), next contact option, no copy online |
| takedown chance/speed, likely, expect, guaranteed times | past notices: about N % removed · usually 3+ days |

`src/i18n/wording.test.ts` scans both resource files for these words; **a hit means rephrase, never
loosen the pattern.** `website/check.py` ports the same patterns for the marketing site — they are
ported, never rewritten.
