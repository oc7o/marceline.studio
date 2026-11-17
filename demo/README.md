# Marceline demo

A static, backend-free build of the real customer app (`../frontend/src`, the `/app` routes) for
GitHub Pages at <https://marceline.studio/>. The screens are imported, not copied; only the API
behind them is replaced.

## Run and build

Node >= 20.19, npm.

```sh
cd demo
npm install
npm run dev        # Vite on http://127.0.0.1:5174/
npm run build      # type check, then the static site in demo/dist
npm run preview    # serves demo/dist like GitHub Pages does, on http://127.0.0.1:4173/
```

`npm run preview -- 8080` picks another port. Publish the contents of `demo/dist` as they are.

## What is mocked

- **The API.** `src/mock/fetch.ts` replaces `fetch` before the app's API client is imported and
  answers every `/api/…` request from `src/mock/state.ts`. No request leaves the page's origin; the
  Content-Security-Policy in `vite.config.ts` enforces that.
- **The data.** One invented organisation ("Novak Studio") with seven originals, nine matched
  pages on `.example` sites, notice drafts in every state, a removals log, invoices and a rights
  record. Addresses come from the documentation ranges. Nothing is real.
- **Writes** change that data in memory: sign out and in (any credentials), approve a draft, mark
  it sent, record a response, declare rights, add a partner or a document, add an original. A
  reload starts over. "Add an original" really fingerprints the chosen file in the browser; the
  file stays on the device and the result is kept only in the tab.
- **Downloads** (invoice PDF, notice exports, "Download my data") are built in the browser by
  `src/mock/downloads.ts`. Links to the sample sites say that the page does not exist.

Not included: the staff moderation screens. Any address outside `/app` leads to `/app`.

## GitHub Pages

`dist/404.html` and `dist/app/index.html` are copies of `index.html`, so a deep link survives a
reload. `public/CNAME` and `public/.nojekyll` are copied into the build. The banner's two links
(source repository, imprint) are constants at the top of `src/DemoChrome.tsx`. Fonts are the
self-hosted OFL files in `public/fonts`, with their `LICENSES.txt`.
