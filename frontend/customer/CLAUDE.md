# The Next.js customer app

Moved verbatim out of `frontend/CLAUDE.md` on 2026-09-18 so that file stays small. The root `CLAUDE.md` hard
rules and `frontend/CLAUDE.md` apply here too.

## The Next.js customer app (`frontend/customer/`, ADR 0036)

Next.js **16.3.5, pinned exactly** (no caret — the budget number depends on it), App Router,
`basePath: "/app"`, `output: "standalone"`, Tailwind v4, its own `package.json`, eslint config and
vitest config. `make customer-dev` / `customer-check` / `customer-smoke`.

**The build is webpack's, deliberately** (`next build --webpack`). Next 16 defaults to Turbopack,
which cannot resolve `@shared/*` — it points outside this project into `frontend/src`, and
`experimental.externalDir` is a webpack feature. `next.config.ts` carries the measurement and the
reason; do not drop the flag to "use the default".

The request-scoped CSP nonce lives in **`proxy.ts`** (Next 16's name for middleware). Nothing that
decides access may go in it — CVE-2025-29927, unchanged by the rename.

```
app/layout.tsx              <html lang> from the request; force-dynamic
app/(signed-in)/…           every screen; the group's layout is the session gate
app/sign-in/page.tsx        the one page outside the gate
app/api/…/route.ts          the BFF: one handler per Django customer endpoint (auth, the
                            fingerprint upload, the invoice PDF, the rights writes, the notice
                            writes) — 21 files, each naming its Django path literally
app/not-found.tsx           the single 404 (unknown / someone else's / a typo read alike)
screens/*.tsx               SYNCHRONOUS server components: data in as props, HTML out
components/*.tsx            the islands ("use client") and the small server primitives
lib/bff.ts                  the BFF's rules as pure functions (paths, headers, write checks)
lib/django.ts               server-only; the only place this app calls Django
lib/reads.ts                one named read per customer endpoint, each memoised per request
lib/translate.ts            the translator: {{placeholder}} + _one/_other via Intl.PluralRules
lib/requestLanguage.ts      cookie -> Accept-Language -> en
lib/write.ts                how an island sends a write, then asks the server for the page again
scripts/check-bundle-budget.mjs   per-route gzip first load, discovered from the build output
scripts/django-upstream.py  a real Django on WSGI (no database), the upstream of the relay test
proxy.ts                    the only proxy/middleware: a per-request CSP nonce, nothing else
```

- **Server components by default.** A screen is a *synchronous* server component that takes its
  data and its words as props; the `page.tsx` above it does the reading and the `notFound()` /
  redirect. That is what lets `screens.test.tsx` render one to a string with no request, and it
  keeps a screen from quietly becoming a client component to reach a provider.
- **The words come as props, never as a module.** `requestI18n()` is `server-only`; an island gets
  `slice(resource, "signIn", "common")`. No resource file is ever a client chunk — measured, not
  assumed: grepping every built chunk for a UI string finds none (issue #99). So the SPA's per-screen
  word slices cost this app nothing: it takes the composed `en.ts` / `de.ts` and renders on the
  server.
- **The language cookie lives 180 days**, and `LANGUAGE_COOKIE_DAYS` in `lib/requestLanguage.ts` is
  the only place that number is written (operator decision 2026-09-18; it was 365). It is written
  only by the customer's own click on the switch — the strongest form of § 25 Abs. 2 Nr. 2 TDDDG's
  "expressly requested" — and the service privacy notice cites that constant, so change it there
  and nowhere else; `lib/languageCookie.test.ts` asserts the value and the whole cookie string.
- **Writes go to `/app/api/*`**, one handler per Django path, and each handler names its path
  literally — `assertCustomerPath` refuses anything outside `/api/v1/app/`. After a write the
  island calls `router.refresh()`; there is no client cache to keep in step.
- **The relay's own answer headers are ours, not the upstream's.** `relay` rebuilds the answer from
  a three-name allowlist (`content-type`, `content-disposition`, `retry-after`), so anything Django
  set for the browser is dropped — that is how the invoice PDF lost its `Cache-Control: private,
  no-store` and its `nosniff`. `RELAY_RESPONSE_HEADERS` (`lib/bff.ts`) therefore states both on
  every relayed answer whatever the upstream said: each one is about one signed-in account, none of
  it may be stored, none of it may have its type guessed. Adding a name to the forwarding allowlist
  means deciding a customer needs it; adding one to `RELAY_RESPONSE_HEADERS` means we always do.
- **Paths:** `next/link` and `router.push` take paths **without** `/app` (`basePath` adds it);
  `fetch` and plain `<a href>` need it (`/app/api/login`, `/app/originals/…`). A route handler's
  path mirrors Django's: `app/api/notices/sandbox-send/[noticeId]` has the verb before the id
  because that is where `ROLE_ONLY_PREFIXES` needs it (ADR 0046) — the relay must never make a
  path the upstream does not have.
- **A write on production is accepted, not applied** (202, ADR 0028 write path, ADR 0046). The
  island renders `isPendingWrite(write.data)` as a factual line and never as "done". A refusal that
  arrives after acceptance is reported by `readNoticeSubmissions` and rendered by
  `NoticeSubmissionsBlock` (issue #86, ADR 0046 §7) on `/app/notices` and
  `/app/originals/{id}/notices`: a plain **server** component, zero client bytes, with a no-JS
  "reload" link in place of a polling timer while something is still processing. Full passage,
  the superseded rule and the measured first loads: `docs/conventions/customer-ui.md`.
- **Budgets**: first load ≤ **150 KB** gzip per route, ≤ **16 KB** beyond the shared entry (operator
  decision 2026-09-18), CSS ≤ 15 KB, and — since issue #99 — the **shared entry alone ≤ 128.5 KB**.
  The last two are the guards that matter: the shared entry is 127.58 KB against ADR 0036's 127.4 KB
  hello-world on this major, so **almost the whole first-load number is the framework**, our own
  client JavaScript is 5.31 KB on a plain route and 11.55 on the worst — which is why the per-route
  budget came down from ADR 0028's 30 KB, a number no route of ours was ever near. Not ADR 0028's
  130 first load either — that was reachable only on Next 15.5, and the operator chose the current
  major knowing it costs ~27 KB (ADR 0036 has both measurements and the arithmetic). Read the
  script's own comments before changing a number. The check discovers routes from the build output,
  so a new screen is measured without touching it. Measured 2026-09-18 on the check runner: read
  routes 139.41 KB, sign-in 140.65 KB, rights 145.54 KB, notices 145.72 KB, add-original 146.28 KB
  (the worst, 3.72 KB of headroom; 12.18 KB of own chunks against the 16 KB per-route budget — the
  upload island, its status polling and `@shared/app/uploadStatus`, ADR 0044), shared entry
  127.58 KB, CSS 6.49 KB. **After phone first** (issue #117, same host, same day): every route
  +0.62 KB — read routes 140.03, sign-in 141.25, rights 146.16, `/app/originals/{id}/notices`
  146.34, add-original 146.90 (still the worst, 3.10 KB of headroom, 12.37 KB of own chunks),
  shared entry unchanged at 127.58, CSS 6.95 KB of 15. The growth is the stylesheet and the
  navigation's own class strings; the new bottom bar added **no** client JavaScript, because a
  media query is not JavaScript. In the SPA: `/app` first load 142.87 KB of 150, core words
  4.07 KB of 4.5 (the five short navigation labels and the skip link). **After photo sets**
  (issue #116, same host, 2026-09-19): only the add-original route moved — **147.80 KB** first load
  and **13.24 KB** of own chunks of 16, so **2.20 KB of headroom** is what is left on the app's
  worst route; every other route is unchanged at 140.05. In the SPA: `/app` first load 143.13 KB of
  150, core words 4.11 KB of 4.5 (one extent sentence), the add-original route 10.73 KB of 16
  (3.15 KB of it words).
- **A photo set is the second kind of original** (issue #116, ADR 0053). `AddOriginalScreen` asks
  what is being added; a set uses the `multiple` `FileDrop` (a discriminated union, so a caller
  cannot read "the file" from a multi picker) and `fingerprintPhotos`, which hashes one photograph
  at a time in the Worker. **Still no picture anywhere** — `lib/privacy.test.ts` is unchanged and
  the customer's own photographs are not an exception. The screen's first load gains only the
  format constants (`@shared/lib/photoFingerprintFormat`, a leaf module of data plus
  `collectionTitle`); the builder and the hasher arrive with the client on start. This is the app's
  worst route — check its number in the budget output before adding anything else to it.
- **The fingerprinting code loads on demand only** (`lib/lazyFingerprinting.test.ts`), exactly as
  in the SPA: `await import("@shared/lib/fingerprintClient")` when the customer presses start — and
  `@shared/lib/videoFingerprint` with it, because the document builder and validator run only then
  too. The screen imports the format constants from `@shared/lib/videoFingerprintFormat`, a leaf
  module of data plus `stem`: importing them from `videoFingerprint` put 2.7 KB raw of pipeline into
  this route's first load (issue #99).
- **Privacy and wording are unchanged and enforced here too**: `lib/privacy.test.ts` scans this
  app's source for anything able to load a remote image, and `src/i18n/wording.test.ts` still
  guards the words both apps read.
- **`lib/routes.test.ts` is the guard rail for all of it**: every page inside the gate group,
  every handler relaying only `/api/v1/app/*`, every `/app/api/…` path an island names having a
  handler, no link leaving `/app`, and every `/app` route the SPA still serves having a page here.
- **The relay is tested against a real Django, never a mock** (`lib/relayDjango.test.ts`, in
  `make check`, ~1.3 s). It starts `scripts/django-upstream.py` — Django with `settings.configure`,
  `CsrfViewMiddleware`, no database — on a loopback port it picks itself, and calls the **real**
  route handlers: the body's bytes and sha256, `Content-Type` and `Content-Length` as Django
  received them, Django's own CSRF verdict on the forwarded `Origin`/cookie/token, and the answer's
  status, `Set-Cookie` and `Retry-After` on the way back. It has to be Django on WSGI: a request
  body there is `LimitedStream(wsgi.input, CONTENT_LENGTH)`, so a relay that drops `Content-Length`
  delivers **nothing** — which is what happened on 2026-09-18 to every write, sign-in included, and
  what a mocked `fetch` and a stub upstream both accepted happily. Delete the `Content-Length` line
  in `lib/bff.ts` and five tests fail with `body_bytes: 0`. `MARCELINE_PYTHON` points at the
  interpreter when `backend/.venv` is not there (a worktree); it is never skipped.
- **On demand, not in `make check`:** `make customer-smoke` runs the production image against a
  stub Django (the gate, the language from the request, the CSP nonce, the whole sign-in through
  the BFF — and its stub now reads the body by `CONTENT_LENGTH` and refuses an empty one, as Django
  does); `make prod-surface-check` exercises the Caddyfile against both values of `CUSTOMER_APP`.
- **"Download my data"** (ADR 0045 amendment 2026-09-18): `Shell` renders a plain same-origin `<a
  download>` to `/app/api/export` (`app/api/export/route.ts` relays to `GET /api/v1/app/export`)
  next to sign-out, only when `me.can_export_data` is true — false, and the link absent, on the
  public host, where the route stays development-only like rights and notices. No client code: the
  server component decides visibility, the browser does the rest with the session cookie.

## Showing us the platform account is yours (ADR 0055) — off by default

- `/app/verification`: `screens/VerificationScreen.tsx` is a **server** component — the standing, the
  code, the claimed accounts and the "what this unlocks" block are HTML, because none of it needs a
  browser. `components/VerificationForm.tsx` is the one island: the claim form and the "I have placed
  it" button, each followed by `router.refresh()`.
- **The claimed profile address is text, never a link** (`screens.test.tsx` asserts no `href` to it):
  the creator typed it, nothing of ours opens it, and a mis-click in a staff or customer screen would
  be the request ADR 0055 promises not to make.
- **No input that could ask for a document or a birth date**, ever: no `type="file"`, no
  `type="date"`, and the screen's first paragraph says we never ask. Two assertions in
  `screens.test.tsx` keep it that way.
- A 404 from Django means the step is not switched on here, which is a sentence (`verification.
  notOnline`), never an error. `me.can_verify_account` is what the rights screen asks before linking
  there, and `state.account_step_next` what the invite's finished panel asks.
- Its words are the `verification` slice in `@shared/i18n/{en,de}/verification.ts`, **not** in
  `src/i18n/slices.ts` — only this app has the screen, exactly like `onboarding`. The rights screen's
  own link text lives in the `rights` slice, because that is the slice that route loads.

## Asking for a copy of your data, or for its deletion (ADR 0056) — off by default

- `/app/data`: `screens/DataRequestsScreen.tsx` is a **server** component — each request's standing,
  its date, the due date, the deadline sentence and the address are HTML.
  `components/DataRequestForms.tsx` is the one island: the two requests and taking either back, each
  followed by `router.refresh()`.
- **A deletion asks for the word the server sent** (`deletion_phrase`), and the button stays disabled
  until it matches. The word is **not translated** — the bundle document a public host serves is
  language-independent, so the check has to be too — and the server checks it twice anyway.
- **An open request offers "take it back", never a second "ask again".** The server refuses a second
  open request of a kind (`already_open`), and a button that always fails is worse than none.
- **Nothing here says data was exported or deleted.** Both are answered by a person; a settled
  request is a record of an exchange in the history, and `screens.test.tsx` asserts it.
- The address comes from the API (`contact_email`), so no string in this app spells it for this
  screen. A 404 from Django means the step is not switched on here, which is a sentence
  (`dataRequests.notOnline`), never an error; `me.can_request_data` is what the shell asks first.
- Its words are the `dataRequests` slice in `@shared/i18n/{en,de}/dataRequests.ts`, **not** in
  `src/i18n/slices.ts` — only this app has the screen, like `onboarding` and `verification`. The
  shell's own link label (`dataRequests.navLink`) lives there too rather than in `core.ts`, although
  the shell renders it on every page: this app renders on the server from the whole composed
  resource, and `core.ts` is what every SPA `/app` visit downloads against a nearly spent 4.5 KB
  budget. It is a plain link beside the export download and sign-out — **not** a sixth navigation
  place, because a fifth of 360 px is 72 px.
- The four BFF handlers under `app/api/data-requests/` each name their Django path **literally**,
  including the two `…/withdraw` ones: a `[kind]` segment would make `lib/routes.test.ts`'s "every
  write an island sends has a handler" check vacuous for this screen.

## Phone first (issue #117, operator decision 2026-09-18)

The audience arrives on a phone. **Every new screen is designed at 360 px first**;
the desktop layout is the `sm:` variant of it, not the other way round. Full passage and the
measured numbers: `docs/conventions/customer-ui.md` "Phone first".

- **Inputs ≥ 16 px and controls ≥ 44 px, and neither is a component's job.** Both are one
  `@media (pointer: coarse)` block in `app/globals.css` (mirrored in `frontend/src/index.css`),
  keyed on the pointer and never on the width — a desktop window narrowed to 360 px is still a
  mouse. Under 16 px **iOS zooms the page in on focus and does not zoom back out**. A standalone
  link opts into the 44 px box with `.tap`; a link inside a sentence never does.
- **The navigation is one markup in two shapes**: a bar fixed to the bottom below `sm`, the tab row
  above it, both from `components/Nav.tsx` with no second DOM and no new island. `Shell` reserves
  its height (`pb-[calc(2.75rem+env(safe-area-inset-bottom))]`), the bar carries the safe-area
  inset itself, and each place has a **short label** (`nav.short.*`) for the bar because a fifth of
  360 px is 72 px and "Entfernungsprotokoll" is not.
- **A table declares its stacked form.** There is no `<table>` in this app today and a new list
  should not introduce one without saying, in the component, how it reads at 360 px: same markup,
  responsive layout (`grid-cols-1 sm:grid-cols-3`, `TITLE_ROW`), never a second DOM for a phone.
- **No hover-only affordance.** `hover:` may only repeat something already stated in the markup —
  an underline, `aria-current`, the focus ring. A phone has no hover.
- **Every empty list says what happens next** (`EmptyState`'s `next` slot): one quiet sentence,
  en + de, and no time in it — `wording.test.ts` bans a promised duration and this is the sentence
  where one is most tempting.
- **`components/islands.test.ts` is the list of client components, by name and with the reason.**
  The bundle budget measures the consequence; this measures the decision. Adding an island is
  allowed and is made deliberately by editing that list — never by deleting the failing name.
- Guards: `components/phoneFirst.test.tsx` (the stylesheet's two floors, the shell's two shapes,
  the short labels' length, the skip link, the next-step sentences), `components/islands.test.ts`.
  **The acceptance test is `docs/runbooks/phone-check.md`** — fifteen checks with a phone in hand,
  because no browser runs on this server or on the check runner.

## Sending the file instead of hashing it (issue #59, ADR 0044) — off by default

- `AddOriginalScreen` offers a second **method** on `/app/originals/new`, shown only where
  `me.can_upload_media` is true (never on the public host, and nowhere while
  `CLOUD_FINGERPRINTING_ENABLED` is off). **Local hashing stays pre-selected**; there is no account
  setting that silently starts sending videos, and the choice is made per file.
- `lib/uploadMedia.ts` is imported with `import()`, exactly like the fingerprinting client, so a
  customer who never chooses it never downloads it. Each chunk is read, fed to a running SHA-256
  (`@shared/lib/sha256`) and sent, then dropped: the file is never held in memory whatever its
  size, and the digest the server verifies is the digest of the bytes that actually arrived.
- Four handlers under `app/api/uploads/`, each naming its Django path literally and validating its
  path parameter against the shape the server generates (`lib/uploadIds.ts`), because a parameter
  reaches `relay` inside a template string. The chunk handler is the only route in this app whose
  body is customer **media**: `relay` streams it through, nothing is buffered, logged or written
  here, and there is no route that reads a chunk back.
- **After the last chunk the screen watches the job** (ADR 0044 amendment 2026-09-18):
  `lib/useUploadStatus.ts` polls `GET /app/api/uploads/{id}` with a backoff and stops on a terminal
  state, on a hidden tab and on unmount, resuming on visibility. Every decision is a pure function
  in `@shared/app/uploadStatus` (`shouldPoll`, `pollDelayMs`, `framesPercent`, `statusKey`,
  `reasonKey`, `saysDeleted`, `openUploads`), so the schedule and the words are tested without a
  browser. The states are queued → fingerprinting (a bar, **indeterminate while `frames_total` is
  null** — never a guessed denominator) → added, with a link to the original → or not added, with
  one sentence per `reason` from the API's closed set and hashing on the customer's own computer
  offered as the way out. The sentence that the file has been deleted is rendered **only** where
  `bytes_deleted` is true.
- **Two tick boxes before a file may be sent** (ADR 0044 amendment 2026-09-19): the uploader is the
  only person in the file, and explicit consent to the processing of it. Shown **only**
  in upload mode — a file hashed on the customer's own computer is not processed by us, so there is
  nothing to consent to — and cleared whenever the method or the file changes, because a tick belongs
  to the file in front of the customer. Under them, the way out: a file with another person in it is
  fingerprinted on this computer instead. The words are the **server's**
  (`backend/marceline/app/upload_declaration.json`) and `src/app/addOriginal.test.ts` fails when the
  two drift apart, exactly as for the rights declaration; `formProblems` refuses to start without
  both (`notDeclared`). Draft wording.
- **The customer may leave**, the words say so, and `components/OpenUploads.tsx` is the other half
  of that promise: a synchronous server component on the overview and the add page listing this
  organisation's uploads still in flight or recently not added, with a `Refresher` only while
  something is moving. Their own uploads only — never our review work.
- The words say what happens to the file and **promise no duration** — `wording.test.ts` bans
  "within N hours/days", "guarantee", "expect" and "likely", and this is exactly the screen where
  one would be tempting.
- Its words are part of the `addOriginal` slice in `@shared/i18n/{en,de}/addOriginal.ts` like the
  rest of that screen's strings (issue #99). They used to live in a module of their own
  (`@shared/i18n/upload.ts`) because the SPA loaded one whole language file and must not download
  words it cannot show; the SPA now loads a screen's words with that screen's route, so the
  exception is gone. `frontend/CLAUDE.md` states the rule for where a new word goes.

## Redeeming an invite (issue #115, ADR 0052) — off by default, and public since 2026-09-19

- **`app/invite/[token]/page.tsx` is the second — and last — page outside the session gate.**
  Whoever opens the link has no account yet, so a gate there would send every invited creator to a
  sign-in they cannot pass. `lib/routes.test.ts` names both ungated pages and fails on a third:
  "outside the gate" is a decision, never a default.
- **One page for every reason a link does not work.** Django answers one identical 404 for unknown,
  malformed, expired, withdrawn, already used and "the feature is off", so `readInvite` throws, the
  page renders `state = null`, and `InviteScreen` shows one paragraph. **Never branch on the
  reason** — this server is not told it, and a second sentence would turn the link into an oracle
  for "was this address invited". `screens.test.tsx` asserts the rendered HTML contains none of the
  distinguishing words, in both languages.
- `screens/InviteScreen.tsx` is a synchronous server component as usual; `components/InviteForm.tsx`
  is the **one island**, one form per step, each posting to a literal `/app/api/invite/{token}/…`
  path (a computed step would make the handler check in `routes.test.ts` vacuous). After every step
  but the last the island calls `router.refresh()` and the server says which step comes next — the
  step is derived from the row on the server (`accounts/invites.step_of`), which is what makes the
  flow resumable. The **last** step is the exception: the invite is used up, so asking again would
  show the neutral page to the person who just finished, and the island renders the finished panel
  from its own state.
- The finished panel says the account is **being looked at** and what can be done meanwhile
  (issue #92: a tick converts nobody). Never render it as "everything is settled".
- **There are two endings, and the server says which** (ADR 0052 §9, slice 2). On the public host
  the last step answers **202 `{"pending": true}`**: the account is created on the machine that
  holds the fingerprints, a minute or two later, and this browser has no session. The island renders
  the `onboarding.pending` panel — what has happened, and a link to **sign-in**, never into `/app`,
  which would bounce off the session gate. `saysPending` reads the answer; **never branch on the
  host name**. `finish` also ignores a submit while one is in flight: the server refuses the second
  redemption, and its neutral 404 would read to the person as a failure.
- **The declaration step shows two wordings with a tick box each** (issue #123, ADR 0052 amendment
  2026-09-19): § 14 BGB and majority/legal capacity, both German and marked `lang`, each with its
  version, and the write sends both versions back so a stale page is refused rather than signing
  text nobody is showing. It is a declaration, **not** a verification: no date-of-birth input, no
  upload, nothing that asks for a document — `screens.test.tsx` counts the boxes and asserts there
  is no date input.
- Refusals arrive as **codes** (`{"problems": ["password_too_short"]}`) and the words are ours, in
  the `onboarding` slice of `@shared/i18n/{en,de}/onboarding.ts`. That slice is **not** in
  `src/i18n/slices.ts`: only this app has the screen, and the SPA is the rollback, not the future.
- The 27 EU country names come from `Intl.DisplayNames` in the browser, so they cost this route
  nothing and need no translation of ours; the ISO code is the fallback.
- `lib/inviteToken.ts` checks the token's shape before it reaches a template string, exactly as
  `lib/uploadIds.ts` does for an upload id — and answers the **same** neutral 404, so the check
  cannot tell a malformed token from an unknown one.
- **Phone first without asking** (issue #117): the inputs, the buttons, the select and the tick
  boxes take their 16 px type and their 44 px hit area from `pointer: coarse` in `globals.css`, and
  the two standalone links (the published document, "add your first work") opt in with `.tap`. The
  finished panel's call to action is `LinkButton` primary + `block` **written out** rather than
  imported: no island here imports `components/ui.tsx`, and importing it would pull the whole
  primitives module into this route's client chunk. The screen is one column inside `max-w-md`, so
  there is nothing to reflow.

## The mailed account actions (issue #149, ADR 0054) — off by default

- **Three more pages outside the session gate**, and `lib/routes.test.ts` names all five:
  `/app/forgot-password`, `/app/reset-password/{token}` and `/app/verify-email/{token}` are reached by
  somebody who has no session and, for a reset, cannot get one. The resend
  (`app/(signed-in)/verify-email`) is inside the gate.
- **A GET confirms nothing.** Mail clients, scanners and link previews fetch links on their own, so
  `/app/verify-email/{token}` renders a **button** and `AccountMailButton` posts. Never turn that into
  a link or a `useEffect`.
- **Nobody is signed in by a reset.** Both endings point at the sign-in form: `pending: false`
  (development, the password is set) and `pending: true` (the public host, it is being set). The
  server says which — `saysPending`, never the host name — and the second ending says so honestly.
- **One neutral page for every reason a link does not work**, exactly as the invite's: Django answers
  one 404, the read throws, the screen gets `state = null`, and `screens.test.tsx` asserts the HTML
  names none of the distinguishing words in either language.
- **The banner that asks for a confirmation is server-rendered** (`Shell`'s `askToVerifyEmail`, from
  `!me.email_verified && me.can_verify_email`): one quiet line and a **link to a page**, so no
  signed-in screen pays client bytes for a button it does not show. It blocks nothing.
- Three islands, all on their own routes (`components/islands.test.ts` has the reasons):
  `ForgotPasswordForm`, `ResetPasswordForm`, `AccountMailButton` — the last shared by the confirm page
  and the resend page, with the BFF path passed in **as a literal from the screen**, so
  `routes.test.ts` still sees it.
- The words are the `accountEmail` slice in `@shared/i18n/{en,de}/accountEmail.ts`, plus three strings
  in `core` (the sign-in link and the banner's two). Not in `src/i18n/slices.ts`: only this app has
  these screens. `lib/accountLink.ts` checks the token's shape before it reaches a template string and
  answers the same neutral 404 — the sibling of `lib/inviteToken.ts`, separate because the refusal
  bodies differ and each must be byte for byte Django's.
