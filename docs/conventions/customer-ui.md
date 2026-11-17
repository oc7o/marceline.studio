# The customer UI in the SPA (`/app`): the full reference

`frontend/src/app/CLAUDE.md` keeps the invariants an agent must not break; this file is the detail
it used to carry — the route/reads table with what every screen renders, the bundle numbers, the
i18n and submission mechanics and the narrative each rule grew out of. Moved here verbatim on
2026-09-18 (issue #79); nothing was removed. The decisions live in ADRs 0023, 0025, 0026, 0027,
0028, 0032, 0033, 0034, 0035 and 0036.

---

# Customer UI in the SPA (`/app`)

Moved verbatim out of `frontend/CLAUDE.md` on 2026-09-18 so that file stays small. The root `CLAUDE.md` hard
rules and `frontend/CLAUDE.md` apply here too.

## Customer UI (`/app`, ADR 0026)
- **In production only `/app`, `/app/*` and `/assets/*` are served** (`infra/prod/Caddyfile`,
  ADR 0028); `/` redirects to `/app`. The staff screens still live in the same `frontend/` project
  and npm build, but not the same bundle a visitor downloads (see "Performance budget" below), no
  staff URL serves the page, and every staff API answers 404 there — so the customer UI must never
  link to or call anything outside `/app` and `/api/v1/app/…` — it would work in development and
  break in production. Never put data or secrets in staff screen code: it still ships to the
  internet, on demand, for anyone who requests its chunk URL directly.
- **Separate audience, separate shell.** `RootShell` (`src/router.tsx`) picks the layout from the
  matched route, so an `/app` URL never renders the staff layout: a customer sees no staff
  navigation and no `GET /auth/me` is even attempted. Staff paths, ids and gate are unchanged.
- **Performance budget (ADR 0028).** Every route in `src/router.tsx` — staff and
  customer, including `RootLayout` and `CustomerApp` themselves — is a lazy route component
  (`lazyRouteComponent`/`lazy`), so the entry chunk carries only React, TanStack Router/Query and
  the route tree's own definitions: a customer never fetches the staff screens, and a staff URL
  never fetches the customer app or its i18n resources. `frontend/scripts/check-bundle-budget.mjs`
  (run by `make frontend-check`) reads the Vite build manifest and fails on a regression: today's
  `/app` first load is ~145 KB gzip (was 188 KB, one bundle, both audiences and both languages,
  before this); ADR 0028 (d.1) explains why its own 130 KB is not yet reachable on this Vite SPA
  and what would close the gap. Per-route budget (30 KB gzip, ADR 0028) is met with room — read the
  script's own comments before changing either number.
- **No pathless layout routes** (`createRoute({ id })`). TanStack types `Link`'s `to` against route
  *ids*, and a pathless parent prefixes every descendant's id — which turns `to="/references"` and
  `to="/app/originals"` into type errors. Add customer screens as children of `appRoute`.
- `CustomerApp` gates every `/app` route except `/app/sign-in`, so a new screen is behind the
  session **by construction**. The ids it compares against live in `src/app/routeIds.ts` and a test
  asserts they still exist in the route tree.
- Auth (ADR 0023): `GET /app/auth/session` **first** — it is the only thing that sets `csrftoken` —
  then `POST /app/auth/login`, `POST /app/auth/logout`. One message per documented status in
  `src/app/signInError.ts` (pure, tested): **401 says exactly one thing** for unknown account,
  wrong password and disabled account alike (never reveal which); 403 distinguishes "no
  organisation" from a stale CSRF token; 429 shows the wait from `Retry-After`
  (`ApiError.retryAfterSeconds`) and never retries (`retry: false` — a lockout is 15 minutes).
- **Every user-visible string comes from `src/i18n`, in German *and* English.** `de.ts` is typed
  `Translation = typeof en`, so a forgotten key fails `tsc`; `i18n.test.ts` fails on any key,
  interpolation placeholder or plural form present in one language only, and on a `t("…")` key the
  resources do not define (it reads both languages through `src/i18n/resources.ts`, a test-only
  module — nothing shipped imports it, on purpose, see below). Language is browser-detected,
  switchable explicitly (the switch is visible before sign-in) and persisted in `localStorage`
  (`marceline.language`).
- **Only the active language ships** (ADR 0028 performance budget): `src/i18n/index.ts`'s
  `ensureI18nReady()` dynamically imports the detected language and initialises i18next with just
  that one; it is the `/app` route's TanStack Router `loader` (`router.tsx`), so no `/app` screen
  renders before it resolves. `useLanguage`'s setter loads the other language, once, the first
  time someone actually switches. Never call `i18n.changeLanguage()` directly (the instance is not
  initialised at import time any more) — tests use `setLanguageForTest` instead.
- **Privacy (hard, issue #53):** the customer UI shows no copy of the pirated material and contains
  nothing *able* to load a remote image — no `img`/`video`/`src`/`url()`/`mediaImageUrl`, no
  absolute URLs. Enforced twice: the eslint block for `src/app/**`, and `src/app/privacy.test.ts`,
  which scans the source text so relaxing the lint config does not quietly relax the ban. If a
  screen ever needs an image, it takes an id for our own passthrough and must be incapable of
  expressing any other URL.
- Tests: `src/app/customerApp.test.tsx` renders the real route tree over a memory history. The
  `vi.hoisted` block there is load-bearing — openapi-fetch captures `globalThis.fetch` at import
  time, and Node's `Request` refuses the relative URLs the SPA uses on purpose. Screen tests share
  fixtures and a stubbed API from `src/app/testApi.tsx`, but each file keeps its own hoisted block.

### Customer data screens (case-file API, ADR 0025)
| Route | Screen | Reads |
|---|---|---|
| `/app` | `HomeScreen`: one card per original (online first), state badge, confirmed count, sites, "monitoring active" + bucket + fixed explainer (no activity or queue indicator), the customer's own uploads being added / not added (only with `can_add_originals`), account card | `GET /app/originals`, `/app/findings`, `/app/fingerprints/submissions` |
| `/app/originals/$originalId` | `OriginalScreen`: case state (open / removed and watching / reopened) and every copy | `GET /app/cases/{id}` |
| `/app/originals/$originalId/copies/$pageId` | `CaseFileScreen`: one copy's case file | the same `GET /app/cases/{id}` |
| `/app/removals` | `RemovalsScreen`: the log, with what "handled" can and cannot mean | `GET /app/removals` |
| `/app/invoices` | `InvoicesScreen` (ADR 0032): issued invoices and cancellations, standing badge, amount (`formatMoney`), dates, VAT note, same-origin PDF link (`invoicePdfPath`); read-only, no bank details on screen (they are on the PDF); development-only for now, so a 404 renders "not shown here yet" (production, ADR 0032) | `GET /app/invoices` |
| `/app/rights` | `RightsScreen` (ADR 0034): claimant type and identity-check state (+ account-control token), per original its rights status and the `notice_blockers` in words, a self-declaration form that shows the **server's declaration text verbatim** and sends its `declaration_version` and language, "withdraw self-declaration" (only while `self_declared`: reason, note, server withdrawal text verbatim), licensed partners with "declare that there are none", proof uploads (multipart `FormData`, no download). Codes → words in `rights.ts` (`errorKey`, code lists; `rights.test.ts` checks both languages and that nothing says "infringement"). Development-only: a 404 renders "not shown here yet" | `GET /app/rights` + its writes |
| `/app/originals/$originalId/notices` | `NoticeDraftScreen` (issue #33, ADR 0035): "Prepare notice draft", linked from the case file's options section (a real link now, not a disabled placeholder). Candidates with their gate blockers in words and a per-finding exception tick for anything not a current full copy; pick pages on one site, a suggested/alternate recipient channel, language and claimant details to create a draft; approve it by ticking every required confirmation (server's **verbatim** text per `required_confirmations`), which freezes `approved_text`; copy button, a `mailto:` link (or the same text to paste into a web form) and three `<a download>` links to `GET /app/notices/{id}/export/{txt|eml|annex}` — the text, an unsent `.eml` for the customer's own mail program, and the evidence annex (`canExport`, `noticeExportPath`, ADR 0035 amendment 2026-09-18); "mark as sent by me" (refused for the sandbox organisation) or, sandbox only, a test-copy send to an allowlisted address; record the provider's response; withdraw or start a corrected draft. Never a "Send" button — Marceline transmits nothing. Codes → words in `notices.ts` (`errorKey`, blocker/status/variant/channel-kind code lists; `notices.test.ts`). Development-only: like rights and invoices | `GET /app/notices/candidates/{id}`, `/app/notices` + its writes |
| `/app/originals/new` | `AddOriginalScreen` (lazy route): video, title, rights holder, co-performers, rights declaration; progress, cancel; offered only when `me.can_add_originals` | `POST /app/fingerprints`, `/app/fingerprints/client-events` |

- **The API sends codes, the UI owns the words.** Everything the screens derive lives in
  `src/app/caseFile.ts` (pure, tested): `leakTimeline` (per copy: `confirmed` as a day (`on`, `formatDay`), `removed`/
  `rechecked` from the copy's own stamps, notice steps **verbatim** from the case timeline),
  `STEP_ACTOR` ("us" / "you" / "the site or its host"), `evidenceOf`, `caseOptions`,
  `hostsOf`. `caseFile.test.ts` asserts every code has words in both languages, because
  template-literal `t()` keys are invisible to `translationKeys.test.ts`.
- **Never invent.** A date is one the API sent. `unavailable` renders as "not available yet" with
  the API's reason, never as upcoming. Options without an endpoint are disabled buttons that say
  why ("Prepare notice draft", never "Send" — #46); an option the API says nothing about reads
  "not offered yet".
- **No match confidence of any kind for customers** (2026-09-17): no band, percentage, evidence
  frame count or distance. The API sends only `evidence: fingerprint_match | no_fingerprint_match`
  per finding, and `evidenceOf` renders that code plus the review day. A customer sees no band
  at all — not even for a hoster (see response history below). Issue numbers are not shown to
  customers.
- **Extent** (issue #62): every finding carries `extent: full_copy | excerpt | not_determined`,
  rendered as a label on the original detail and as label plus one sentence under "Where it is"
  in the case file (`finding.extent.*`, `COPY_EXTENTS` in the words test). A fact about the copy,
  never a confidence; excerpts are findings like any other.
- **Customers never see our work in progress** (2026-09-17): the API sends confirmed copies only, so
  an unconfirmed finding is invisible — no "being checked" state, badge or count anywhere, and no
  first-seen date. Dates of our internal steps (`confirmed_on`, `opened_on`, `reopened_on`) are
  date-only and render without a time (`formatDay`).
- **Coverage is a bucket, never a list or a count** (operator, 2026-09-16): the overview and the
  removals log say "we watch 10+ sources" from `monitored_sources` on `GET /app/me`. Never render
  which sites we crawl; a finding's own site and URL are fine.
- **Hoster cooperation is a rounded, past-tense response history only** (operator, 2026-09-17; ADR
  0027 amendment): `response_history` is null ("No response history yet") or
  `{removed_percent, usual_removal_time}`, rendered by `ResponseHistoryLine` as "About 75 %
  removed · usually 3+ days" (`REMOVAL_TIMES`: same_day … 30_plus_days) plus a "rounded, so far"
  note. The API already rounds; never compute, sharpen or colour it into a verdict, never show
  counts, and never bring back chance/speed bands or forecast words.
- **Hosting (ADR 0027)** renders each finding's `hosting` (`HostingSection`, `hostsOf`): page host
  first, then preview hosts once each. Up front: provider, country (`Intl.DisplayNames`) and the
  **suggested notice channel** for the `jurisdiction` code (DSA Article 16 / DMCA / the provider's
  abuse contact — never "DSA applies"), the CDN in front, the abuse contact with its source, the
  registrar, the response history. IP addresses and AS number only inside a collapsed
  "technical details". RDAP source records are text, not links. `not_looked_up` says so and
  renders no empty rows; a missing fact reads "not known yet".
- **Matched page addresses** are text; only the record (`CaseFileScreen`) links to one
  (`PageAddress`: http(s) only, `target="_blank" rel="noreferrer noopener"`). The overview, detail and log never link out.
- **Ids are public ids** (ADR 0033): `$originalId` is `orig_` + 32 hex, `$pageId` is `page_` + 32
  hex, an invoice `id` is `inv_…`, and `organisation.id` is `org_…` — strings, never database keys. Route params go through
  `parsePublicId(kind, value)` (`caseFile.ts`) before any request; anything else, an old integer
  URL included, renders `NotFound` without asking. Pass ids to `params` as they are (no
  `String()`/`Number()`). Test fixtures: `originalId(n)` / `pageId(n)` / `invoiceId(n)` (`testApi.tsx`).
- Query states: `Loading`, `LoadError` (no server text, a retry button), and `NotFound` for a 404
  or a copy not among the original's findings — the same screen, so a safety-hidden page or another
  organisation's case looks exactly like a typo. Customer reads never retry a 4xx.

### Notice submissions block (issue #86, ADR 0046 §7)
Before this was built, both apps stopped at the pending line: a notice write on production is
accepted (202) and applied on development shortly after, and a refusal that arrives *after*
acceptance had nowhere to show. The passages that used to say so, verbatim:

- SPA (`frontend/src/app/CLAUDE.md`, as ADR 0046 left it): "A refusal that arrives *after*
  acceptance is reported by `GET /app/notices/submissions` (`useNoticeSubmissions`,
  `isUnsettled`), which **no screen renders yet**: the words did not fit the 150 KB first-load
  ceiling, and the block belongs in the Next app once the SPA stops shipping them (ADR 0046 §7)."
- Next app (`frontend/customer/CLAUDE.md`, as ADR 0046 left it): "A refusal that arrives after
  acceptance is reported by `readNoticeSubmissions`, which **no page reads yet**: a block for it
  needs words, and words ship in `/app`'s first load, which was at its ceiling while the SPA still
  shared one whole language file (ADR 0046 §7). Issue #99 changed that — the words are per screen
  now, the SPA's first load is 142.65 KB of 150 and a `notices` block's words cost the notices
  routes rather than every visitor. Room for it, measured 2026-09-18: in the SPA **5.0 KB** per
  route (the draft route is 10.96 KB of 16) and nothing off the first load; here **10.6 KB** on
  `/app/notices` (139.38 KB of 150) but only **4.3 KB** on `/app/originals/{id}/notices`, which is
  the app's worst route. Build it here first, and on the list route if it fits either."
- Backend (`backend/marceline/notices/CLAUDE.md`): "`GET /app/notices/submissions` is the
  production-local read that reports it; **no screen renders it yet** — `/app`'s first load is at
  its 150 KB ceiling and the words did not fit (ADR 0046 §7), so a refusal after acceptance is
  visible to staff and not to the customer."

Issue #99 (per-screen words) made the room this needed; issue #86 builds it. `NoticeSubmissionsBlock`
(one component per app, same name) renders `GET /app/notices/submissions`, filtered to
`isUnsettled` (a settled write needs no line — the draft itself, open on the same list, is the
record). It sits on `/app/notices` and `/app/originals/{id}/notices` in both apps:

- **SPA** (`src/app/NoticeSubmissionsBlock.tsx`): a normal hook-driven component using the existing
  `useNoticeSubmissions()` (polls every 15 s while something is `processing`, same as the upload
  submissions block). Its own `FILE_SLICES` entry in `translationKeys.test.ts` gives it the
  `notices` slice, since it is not itself one of the screens `slices.ts` names.
- **Next app** (`components/NoticeSubmissionsBlock.tsx`): a plain **server** component — zero
  client bytes — fed the read as a prop from each page. No polling: a `refreshHref` prop renders a
  plain, no-JS `<a>` back to the same page while something is still `processing`, in place of a
  client-side timer (`Refresher`, which the upload-submissions block on the overview does use, is
  deliberately not reused here — the words did not need it and a route this close to budget should
  not spend client JS on it either).
- **Codes**: `finding_not_eligible` / `finding_no_longer_eligible` used to be the only word a
  refused create/approve could carry after acceptance, and it says only "something changed,
  reload" — true but useless when the customer cannot see why. `customer_writes.py
  _submission_code_for` narrows it, from the very same blocker codes the live create/approve
  screen already shows inline: a refusal whose blockers are all on the rights side becomes
  `finding_rights_blocked` ("check it on the rights page"), one whose blockers are only the page's
  own availability becomes `finding_unavailable` ("nothing more for you to do here"), and anything
  mixed keeps the generic code. No schema change — `NoticeSubmissionOut.code` was already a plain
  string. Every other refusal a customer can actually cause (`notice_not_found`, `unknown_page`,
  `not_draft`, `already_closed`, …) reuses the very sentence the live screen already has for it
  (`codeKey`); anything without words of its own reads a neutral line with the support contact,
  never the code.
- Measured first loads with the block built (check-runner, 2026-09-18, `make remote-check` on
  branch `notice-submissions-block`): SPA total unchanged at 142.70 KB of 150 (route-loaded words,
  no first-load cost) — its own routes grew inside budget, `/app/notices` to 7.97 KB of 16 and
  `/app/originals/{id}/notices` to 11.95 KB of 16. On the Next app the server component cost
  nothing: `/app/notices` stayed 139.41 KB first load / 5.31 KB own chunks, identical to a plain
  route; `/app/originals/{id}/notices` (the island route) moved 145.55→145.73 KB / 11.55→11.63 KB,
  all of it the shared module's own growth. Full numbers: ADR 0046's amendment.

### Add an original (issue #29)
- **The video never leaves the computer**, and the fingerprinting code never loads with the app:
  the route is `lazyRouteComponent`, and the screen `import()`s `lib/fingerprintClient` when the
  customer presses start (Worker + PDQ WASM follow from there). `lib/lazyFingerprinting.test.ts`
  fails on a static import of `fingerprintClient`, `fingerprintWorker` or `pdq` anywhere else —
  use `import type`, or `lib/fingerprintProgress.ts` / `lib/videoFingerprint.ts` for pure helpers.
- Logic in `src/app/addOriginal.ts` (pure, tested): `castFrom` (rights holder first, names once),
  `formProblems`, `localFailure` / `uploadFailure` (error codes → `addOriginal.errors.*` keys, never
  server text), `RIGHTS_TEXT_VERSION`. The declaration's words (`addOriginal.rights.statement`) must
  equal `backend/marceline/app/rights_confirmation.json` for that version — `addOriginal.test.ts`
  reads the file. New wording = a new version on both sides, never an edit.
- Progress re-renders only on whole-percent changes; digest and hashing are shown as two bars
  because they run at the same time. A cancel is not counted as a failure; codec and other local
  failures are posted as client events with the decoder message and MIME type, never a file name.
- **Production accepts first, adds shortly after** (ADR 0028 write path). `POST /app/fingerprints`
  answers 202 `{submission}` there (`isAccepted`, `src/app/submissions.ts`, pure and tested): the
  page says the original was received and is being added ("being added" / "wird hinzugefügt") and
  links back to the overview — never "open the original", there is none yet. The overview lists
  the organisation's submissions that are processing, refused or failed (`openSubmissions`; added
  ones are simply originals), polls every 15 s only while one is processing (`useSubmissions`) and
  refetches originals and findings when one settles (`settledSince`). Refusals reuse the upload
  errors' words (`submissionMessage`); `SUBMISSION_KEYS` must exist in both languages. This is the
  customer's own upload, never our review work: no "being checked", no counts of anything else.
  503 `processing_paused` has its own sentence (nothing was saved, try later).

### Wording rules (customer UI, operator 2026-09-17)
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

`src/i18n/wording.test.ts` scans both resource files for these words; a hit means rephrase, never
loosen the pattern. Notices are drafts the customer reviews and sends (`Prepare notice draft`).

---

## Loading the words per screen (issue #99, 2026-09-18)

The condensed rule is in `frontend/src/app/CLAUDE.md`; the mechanism and the measurements are in
`docs/conventions/frontend.md` ("How the customer words reach a page") and the ADR 0036 amendment.
What a reader of *this* file needs:

- `ensureI18nReady()` loads the detected language's **core** slice and is still the `/app` route's
  loader. Five screens have words of their own and a loader of their own —
  `loadScreenWords("CaseFileScreen" | "AddOriginalScreen" | "RightsScreen" | "NoticesScreen" |
  "NoticeDraftScreen")` — declared once in `src/i18n/slices.ts`.
- **A new customer screen**: put its words in a new slice under `src/i18n/en/` and `src/i18n/de/`,
  name it in `slices.ts`, and give its route the loader. Words the shell or the overview shows go in
  `core.ts` instead; `CORE_WORDS_BUDGET_KB` (4.5 KB gzip) is what fails if that line is crossed.
- The failure this design makes possible is a screen naming a key from a slice its route never
  loaded — i18next would render the key itself to a paying customer, and neither `tsc` nor the
  merged-resource tests would see it. `src/app/translationKeys.test.ts` checks every `t("…")`,
  every `key: "…"` and the static head of every template-literal key, per file, against exactly the
  words that file's route has.
- The bundle numbers this bought, and the numbers the script now guards separately (the `/app` first
  load, the framework entry, the core words): `frontend/scripts/check-bundle-budget.mjs` and the
  ADR 0036 amendment of 2026-09-18.

The sentence this replaces is the "Only the active language ships" bullet in this file's own
Language section above, kept as it was written: it is still true of the language, and now also of
the slice.

## Phone first (issue #117, operator decision 2026-09-18)

The audience changed: independent creators who arrive from Instagram, on a phone. Both customer
apps were built and reviewed on a desktop, and the audit that opened issue #117 found the damage
was **not** where it was expected. There is no `<table>` anywhere in either app; the cards already
stack, both `dl` grids were already `grid-cols-1 sm:grid-cols-N`, `TITLE_ROW` already put a badge
on its own row below 381 px, and `PageAddress` already wrapped a long address with `min-w-0` +
`break-all`. What was actually broken was the **frame** and the **controls**.

### The two floors, in CSS, keyed on the pointer

`frontend/customer/app/globals.css` (and the same block in `frontend/src/index.css`) holds one
`@media (pointer: coarse)` rule set. It is stated there and not in a component for a reason that is
organisational as much as technical: most of the controls in the customer app live in screens owned
by whoever owns that feature, and a rule that has to be remembered per component is a rule that
gets dropped. A stylesheet reaches every button in the app, including the ones written afterwards.

- **`input`, `select`, `textarea` → `font-size: 16px`.** Under 16 px, Safari on iOS zooms the page
  in when the control takes focus and **does not zoom back out**; the customer spends the rest of
  the visit on a page a third too wide. The app's control sizes were 13 px (the `INPUT` constants
  in `NoticeDraftScreen` and `RightsScreen`) and 15 px (the sign-in form), so *every* input in the
  app triggered it. This is not a change to the type scale — it is the size at which a
  finger-driven browser stops interfering with the page.
- **`button`, `select`, `summary`, `[role="button"]`, `a[download]`, `.tap` → `min-height:
  2.75rem`.** The system's controls are `py-1.5` around a 14 px label: 30 px, fine for a mouse and
  a miss for a thumb. `button` is safe to floor blanketly because a `<button>` in this app is
  always a control and never a word in a sentence. `a[download]` is named because the three notice
  exports and the invoice PDF are plain underlined links and a mis-tap there costs a file.
- **`input[type=checkbox|radio]` → 20 px.** A tick box is drawn by the browser at about 13 px and
  cannot be padded; its label does the rest, and every tick box here sits inside one.
- **`.tap`** is the opt-in for a *standalone* link — "What has happened", "Open the record", a
  pagination step, a link drawn as a button. Opt-in, marked at the link, because only the screen
  knows whether a link is an action or a word in a paragraph: a 44 px box around a link inside a
  sentence breaks the sentence.

`pointer: coarse` rather than a width, everywhere: it asks whether the primary input is a finger,
which is exactly the population both failure modes belong to, and it leaves a desktop window
narrowed to 360 px alone.

### The navigation: one markup, two shapes

`components/Nav.tsx` is a bar fixed to the bottom of the screen below `sm` and the tab row it
always was from `sm` up. The same list, the same links, the same island, no second DOM for a phone
and no JavaScript that measures anything — a media query does all of it, so the client bundle did
not change at all.

- `Shell` reserves the bar's height at the bottom of the page
  (`pb-[calc(2.75rem+env(safe-area-inset-bottom))] sm:pb-0`) and only while signed in, because the
  bar only exists then. The safe-area inset is added *inside* the bar as well, under the last row
  of text, which is where an iPhone's home indicator sits.
- The "you are here" marker is a `brand` rule along the edge the bar is attached to: the top edge
  at the bottom of the screen, the bottom edge under the header. `aria-current` says the same
  without colour.
- Each place has a **short label** (`nav.short.*` in `core.ts`) for the bar. A fifth of 360 px is
  72 px; "Entfernungsprotokoll" is twenty characters and would either wrap into something
  unreadable or push the page sideways. Both spans are in the DOM and one of them is
  `display: none` at any width, so exactly one reaches the accessibility tree and nothing is read
  twice.

### The frame, cut to what a phone needs

The header dropped the tagline and the "signed in as …" line below `sm` (both come back at `sm`),
its padding went from `px-6 py-3` to `px-4 py-2.5`, and `main` went from `p-6` to `p-4 sm:p-8` —
24 px of gutter on each side of a 360 px screen is 13 % of it. The sign-in card's negative margins
follow `main`'s padding and were changed with it. A **skip link** is now the first thing in the tab
order, into `<main id="main" tabIndex={-1}>`: without it a keyboard reader walks the language
switch, the sign-out button and five navigation items before every single screen.

### Empty states say what happens next

A new creator meets four empty screens before the product has anything to show, and "there is
nothing here" on its own reads as a product that is not working. `EmptyState` has a `next` slot —
one quieter paragraph, after the body, marked `data-empty-next` so a test can find it — filled on
the overview, the removals log, the invoices and the notices list, in both languages. It says
nothing about **when**: `wording.test.ts` bans a promised duration, and this is exactly the
sentence where one is most tempting.

### The add-original entry is honest, to everybody

Hashing a video happens in the customer's own browser, which means reading the whole file: slow on
a phone, and beyond some mobile browsers entirely once the file is large. `addOriginal.onAPhone` is
rendered by `app/(signed-in)/originals/new/page.tsx` above the form — **server-rendered, always
visible, the same text on every device**. Not a note that appears only on a phone: deciding that
would mean reading the User-Agent or a client hint, which is a request-time fingerprinting signal
this product does not take, and it would mean an island on that route to measure the viewport. A
desktop customer reads it and shrugs; a phone customer who is not told loses an hour. It promises
nothing about sending the file instead — that option is ADR 0044's and is off.

### Contrast, computed

Every pair this work touches, both themes, WCAG 2.1 (normal text needs 4.5:1, a non-text element
3:1):

| Pair | Light | Dark |
|---|---|---|
| navigation label, inactive (`surface-200` / `ink-muted`) | 6.27:1 | 7.98:1 |
| navigation label, active (`surface-200` / `brand-ink`) | 6.63:1 | 5.52:1 |
| the "you are here" rule (`surface-200` / `brand`) | 5.87:1 | 3.42:1 |
| "what happens next" (`surface-100` / `ink-subtle`) | 6.60:1 | 5.81:1 |
| skip link (`surface-100` / `brand-ink`) | 8.05:1 | 5.90:1 |

The rule is the only one near a floor, and it is a 3:1 element that never carries the state alone.

### What is guarded, and what is not

`components/phoneFirst.test.tsx` asserts the stylesheet's two floors and that they are keyed on the
pointer, the shell's two navigation shapes, the safe-area reservation, five 44 px cells, the short
labels' length in German, the skip link into a focusable `main`, the gutters, and that every
"what happens next" sentence exists in both languages and promises no time and no outcome.
`components/islands.test.ts` is the list of client components by name with the reason for each: the
bundle budget measures the *consequence* of an island, and an island of 300 bytes passes it while
still turning a server-rendered screen into one that waits for React.

**None of that is a phone.** No browser runs on the development server or on the check runner, so
every automated guard here asserts the shape of the markup and the stylesheet — the decision a
phone will render, never what a phone does with it. Font metrics, the keyboard's height, Safari's
zoom, the home indicator and a thumb's reach are measured by holding a phone:
`docs/runbooks/phone-check.md`, fifteen checks, and that is the acceptance test.

### The SPA copy gets the CSS half only

`frontend/src/index.css` carries the same two floors and nothing else. The SPA's `/app` screens
(`src/app/**`) are on Tailwind's own palette deliberately, are ADR 0028's rollback for
`CUSTOMER_APP=spa`, and are on their way out with ADR 0036: they are not re-laid-out here, because
the rollback's value is that it is the thing that already worked. Route parity (`routes.test.ts`)
is untouched.
