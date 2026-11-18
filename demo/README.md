# Marceline demo

A static, backend-free build of the real customer app — the Next.js project in
`../frontend/customer` — for GitHub Pages at <https://marceline.studio/>. Its pages, screens,
components, route handlers and stylesheet are imported, not copied; only the layer underneath them
is replaced.

## Run and build

Node >= 20.19, npm.

```sh
cd demo
npm install
npm run dev        # Vite on http://127.0.0.1:5174/
npm run build      # type check, then the static site in demo/dist
npm run preview    # serves demo/dist as GitHub Pages would, on http://127.0.0.1:4173/
```

`npm run preview -- 8080` picks another port. Publish the contents of `demo/dist` as they are: it
carries `CNAME`, `.nojekyll`, the app under `/app` and the fonts with their licences in
`/app/fonts`.

## How it works

The customer app renders on a server and reads Django. A static host has neither, so:

- **The pages run in the browser.** `src/runtime/router.tsx` calls the app's own `page.tsx`
  functions and its session-gate layout, and renders what they return. `next/link`,
  `next/navigation` and `next/headers` are small stand-ins in `src/runtime/` (the Vite config
  aliases them); the language is still the app's cookie, read by the app's own rule.
- **One module is swapped:** `frontend/customer/lib/django.ts`, the only place the app talks to
  Django, becomes `src/runtime/django.ts`, which answers from `src/mock/`.
- **The route handlers are the real ones.** An island posts to `/app/api/…` as in production;
  `src/runtime/bff.ts` hands the request to the handler in `frontend/customer/app/api`.
- **Every address is the same document.** `/app`, each fixed route and each sample row's address
  get a copy of it, and `404.html` is one more, so a deep link survives a reload. `/` is the
  demo's own front page (`src/Home.tsx`, with the design system's tier icons); any other address
  outside `/app` is a real redirect to `/app/`: the app's language cookie is scoped to `/app`, and
  only a document loaded from there can read it.

Nothing in `frontend/` is changed for the demo. The sentences of two flows the demo does not have
(the invite's declaration and the one for sending a file to a server) are left out of the built
dictionaries; `vite.config.ts` lists them.

## What is mocked

- **The data.** One invented organisation ("Novak Studio") with seven originals, nine matched
  pages on `.example` sites, notice drafts in every state, a removals log, invoices, a rights
  record, an account claim and a data request. Addresses come from the documentation ranges.
  Nothing is real.
- **Writes** change that data in memory: sign out and in (any credentials), approve a draft, mark
  it sent, record a response, declare rights, add a partner or a document, claim an account, ask
  for a copy of your data or take the request back, add an original. A reload starts over, with
  one exception: an original you added keeps its row (title, kind, length) in the tab's session
  storage, so its own address still opens after a reload. "Add an original" really fingerprints
  the chosen file in the browser; the file stays on the device.
- **Downloads** (invoice PDF, notice exports, "download my data") are sample files generated in
  the browser. A download link opened in a new tab lands on the overview and saves the file from
  there, since a static host has nothing at `/app/api/…`.
- **Mail does not exist.** "Forgot password" says so; the pages a mailed link would open
  (`/app/verify-email/{token}`, `/app/reset-password/{token}`) work with any token. The sample
  address counts as confirmed, so nothing links to `/app/verify-email`.
- **Not shown:** the invite redemption (`/app/invite/{token}` answers "this link does not work",
  as the app does for any unknown link) and sending a file to be fingerprinted on a server, which
  is off here as on the public host.

No request leaves the page's origin; the Content-Security-Policy in `vite.config.ts` enforces it.
