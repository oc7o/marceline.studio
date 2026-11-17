/**
 * The customer app (`/app`, ADR 0036). Everything it serves lives under `/app`, including its own
 * static chunks (`basePath`), so production's public allowlist is still exactly the two paths
 * `/app` and `/app/*` that ADR 0028 names — Caddy needs no new route for the framework.
 *
 * **Bundler: webpack, chosen explicitly** (`next build --webpack` in `package.json`). Next 16 makes
 * Turbopack the default, and the choice is not about speed:
 *
 * - Turbopack cannot resolve the `@shared/*` alias, which points **outside** this project into
 *   `frontend/src`. `experimental.externalDir` is a webpack feature; measured 2026-09-17,
 *   `turbopack.root`, an exact `resolveAlias` and a wildcard `resolveAlias` each still left 26
 *   unresolved `@shared/…` imports. Switching would mean duplicating ~2,100 lines of tested logic
 *   or moving them out of the SPA — and that shared single copy is the only thing keeping the two
 *   customer apps from drifting while both exist.
 * - Webpack is also the smaller of the two: a hello-world App Router page measured 127.4 KB gzip
 *   on webpack against 129.9 KB on Turbopack (ADR 0036's table).
 *
 * Revisit when the SPA's `/app` half is deleted and the shared modules can simply move in here.
 */
import type { NextConfig } from "next";

const config: NextConfig = {
  basePath: "/app",
  // A Node server, not a Vercel deployment: `infra/prod/customer.Dockerfile` copies the
  // standalone output and runs `node server.js`.
  output: "standalone",
  // No `X-Powered-By`, and no build id in a header a stranger can read.
  poweredByHeader: false,
  reactStrictMode: true,
  // Development only. The dev server binds to WEB_BIND (a Tailscale address) but the operator
  // reaches it by hostname, and Next 16 blocks its own dev chunks when the Host differs from the
  // bind address. Nothing hydrates then: every island is inert markup, which reads as "the page is
  // slow and the buttons do nothing" (2026-09-18). Has no effect on a production build.
  allowedDevOrigins: ["dev.example.internal", "100.64.0.1", "127.0.0.1"],
  // Next 16 writes an AGENTS.md and a CLAUDE.md into this folder on every dev start. A generated
  // CLAUDE.md here would be loaded as project instructions by any coding agent working in the
  // customer app — instructions nobody on this project wrote or reviewed. Off (2026-09-18).
  agentRules: false,
  // The words and the tested pure logic live once, in `frontend/src` (ADR 0036), and are imported
  // through the `@shared/*` alias while the Vite SPA still serves /app as the rollback. See the
  // bundler note above: this is why the build is webpack's.
  experimental: { externalDir: true },
  // Two lockfiles live under frontend/ (the staff SPA's and this app's); say which root the
  // standalone output traces from instead of letting Next guess.
  outputFileTracingRoot: import.meta.dirname,
  // There are no images in the customer UI (issue #53) and the optimiser must never be reachable.
  images: { unoptimized: true },
  // Meta's PDQ hasher is Emscripten output (`src/wasm/pdq-wasm.js`, ADR 0017) whose Node branch
  // does `await import("module")` behind a `typeof process` check. Nothing in a browser reaches
  // it, but webpack still resolves the specifier — so say there is no Node build of these.
  webpack: (config: {
    resolve: { fallback?: Record<string, false | string> };
  }) => {
    config.resolve.fallback = {
      ...config.resolve.fallback,
      module: false,
      fs: false,
      path: false,
      url: false,
      crypto: false,
    };
    return config;
  },
  // Nothing here is ever framed, and no browser may guess a content type.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Robots-Tag", value: "noindex, nofollow" },
        ],
      },
      // The self-hosted typefaces (`public/fonts/`, issue #84) — the woff2 files only, never
      // `LICENSES.txt` beside them, which carries no hash and can change without a new URL. Next
      // serves `public/` with `Cache-Control: public, max-age=0` by default, so a server-rendered
      // app — every navigation is a document request — re-validated all three woff2 files on every
      // page view: three 304 round trips per view. A year-long, `immutable` header removes that,
      // and is only honest because the filename carries a content hash (`app/globals.css`, checked
      // against the file's own bytes by `app/tokens.test.ts`) — a changed file is a changed URL,
      // never stale bytes served for a year with no way to bust them.
      //
      // Set here, not in `infra/prod/Caddyfile` (which already does the same thing for the SPA's
      // `/assets/*`): `basePath` prefixes this `source` with `/app` automatically, so the same rule
      // matches `/app/fonts/*.woff2` wherever this app runs, including `make customer-dev`, which
      // has no Caddy in front of it at all — a Caddy-only rule would leave development
      // re-validating forever. One rule, no production-edge change needed.
      {
        source: "/fonts/:file(.*\\.woff2)",
        headers: [{ key: "Cache-Control", value: "public, max-age=31536000, immutable" }],
      },
    ];
  },
};

export default config;
