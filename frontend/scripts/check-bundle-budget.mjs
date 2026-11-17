#!/usr/bin/env node
/**
 * Bundle-size budget for the customer app **as this SPA serves it** (`/app`, ADR 0028 d.1,
 * issue #82).
 *
 * Since ADR 0036 the customer app is a Next.js project (`frontend/customer/`) with its own,
 * stricter check (`frontend/customer/scripts/check-bundle-budget.mjs`, 130 KB first load, measured
 * 109.6 KB). This one still runs, because the SPA's `/app` routes are the rollback
 * (`CUSTOMER_APP=spa` in `infra/prod/.env`) and must not rot while they are: it guards the phase-1
 * ceiling on what the Caddy image would serve if the operator switched back. It goes away with
 * `src/app/**`.
 *
 * Reads the Vite build manifest (`dist/.vite/manifest.json`, produced by `vite build` with
 * `build.manifest: true`) so this reasons about the *real* dependency graph rather than chunk
 * file names: which chunks `index.html`'s entry script pulls in synchronously (downloaded by
 * every visitor, staff or customer), and which chunks each `/app` route's lazy component pulls in
 * on top of that. Run after `npm run build`.
 *
 * Two things are enforced (see the "Phase 1 numbers" comment below for why the totals are what
 * they are, not ADR 0028's Next.js-era 130 KB/30 KB figures verbatim):
 *   - every additional `/app` route stays at or under ADR 0028's per-route budget (30 KB gzip);
 *   - the total first load of `/app` (shared entry + the customer shell + the **core** words +
 *     the Home route, i.e. what a fresh visitor's browser actually downloads before the overview
 *     paints) does not regress past FIRST_LOAD_BUDGET_KB;
 *   - and, since issue #99, the two baselines behind that total — the framework entry and the core
 *     words — each stay at their own stated number, so a trimmed screen cannot quietly make room
 *     for a library or for a screen's words moving into the core file.
 */
import { readFileSync, existsSync } from "node:fs";
import { gzipSync } from "node:zlib";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const DIST = path.join(ROOT, "dist");
const MANIFEST_PATH = path.join(DIST, ".vite", "manifest.json");

// --- Phase 1 numbers (measured 2026-09-17, Vite SPA, ADR 0028) -----------------------------
//
// ADR 0028 sets ≤130 KB gzip first load / ≤30 KB gzip per route for the **Next.js** customer app
// (phase 2), where server components ship no React Query and no client router for most of the
// page. This Vite SPA (phase 1) renders everything client-side, so it always pays a fixed,
// unavoidable framework floor before a single screen of ours runs:
//
//   react + react-dom (production, client)  ~68.5 KB gzip
//   @tanstack/react-router (core, no devtools)  ~26.3 KB gzip
//   @tanstack/react-query (core)                 ~7 KB gzip
//   -------------------------------------------------------------------
//   floor                                       ~102 KB gzip, before i18next, the API client or
//                                                any component of ours
//
// (measured by building `react-dom/client` + `createRoot`, `@tanstack/react-router`'s
// `createRouter`, and `@tanstack/react-query`'s `QueryClient` in isolation with this project's
// `vite build`). Add i18next + react-i18next (~14 KB), the typed API client (~7.5 KB) and the
// customer words for the shell alone, and 130 KB total is not reachable while
// staying on full client-side rendering — the same conclusion ADR 0028 draws for Next.js's own
// ~100 KB floor, one rung further down the stack. Closing that gap needs ADR 0028's phase 2
// (server components; no React Query, no client router shipped for most of the page) or a
// lighter router/query stack, both bigger changes than splitting the existing SPA.
//
// What phase 1 *can* fix, and what this script enforces: the staff moderation screens and the
// inactive language no longer ship to `/app` at all (was 188 KB gzip, one bundle, both audiences
// and both languages, always). FIRST_LOAD_BUDGET_KB is today's measured total plus a little
// slack, so this script still catches a regression; ROUTE_BUDGET_KB is tighter than ADR 0028's own
// per-route number, for the reason stated with it below.
const FIRST_LOAD_BUDGET_KB = 150;
// **Per-route budget: 16 KB, not ADR 0028's 30** (operator decision 2026-09-18, issue #99). The
// per-route number is the only one of these that measures our own code: the first-load total is
// dominated by the framework floor, so it cannot fail on a screen doubling in size. Our worst route
// spends 11.55 KB (the Next app) and 10.71 KB (this SPA) of its own, and none has ever been near 30,
// so 30 guarded nothing. 16 is the worst route plus ~4.5 KB — about what a screen grows by when it
// gains a form or a table — and the totals stay at 150.
const ROUTE_BUDGET_KB = 16;

// --- The two baselines, so a win cannot erode silently (issue #99) --------------------------
//
// The first-load total is one number over four terms, and three of them are not screens of ours.
// Trimming a screen therefore makes room for a library to move in unnoticed. These two guards say
// what each baseline is allowed to be, so the total can only be blamed on the thing that grew.
//
// `ENTRY_BASELINE_KB` is the framework floor: React, ReactDOM, TanStack Router and Query, plus
// whatever else `index.html`'s entry pulls in synchronously. Measured 105.75 KB gzip on
// 2026-09-18, and this is that plus ~1 KB. **A dependency added to the entry graph fails here
// first**, which is the point; raising it means saying in this comment what the bytes buy.
const ENTRY_BASELINE_KB = 107;
// `CORE_WORDS_BUDGET_KB` is the words every visitor pays for (`src/i18n/*/core.ts`): the shell,
// the sign-in form, the overview and the small screens. A screen's own words belong in that
// screen's slice, so this failing means words were put in the core file that only one screen
// shows — move them to a slice (`src/i18n/slices.ts`) rather than raising this.
const CORE_WORDS_BUDGET_KB = 4.5;

// Which manifest source key is the customer shell, and which are its routes ("Home" is folded
// into the first-load figure — it is what a fresh visitor to `/app` actually renders — the rest
// are the *additional* routes ADR 0028's per-route budget is about).
const SHELL = "src/app/CustomerApp.tsx";
const FIRST_LOAD_ROUTE = "src/app/HomeScreen.tsx";
const OTHER_ROUTES = {
  "SignIn (/app/sign-in)": "src/app/SignInScreen.tsx",
  "Original (/app/originals/$id)": "src/app/OriginalScreen.tsx",
  "CaseFile (/app/originals/$id/copies/$id)": "src/app/CaseFileScreen.tsx",
  "Removals (/app/removals)": "src/app/RemovalsScreen.tsx",
  "Notices list (/app/notices)": "src/app/NoticesScreen.tsx",
  "Invoices (/app/invoices)": "src/app/InvoicesScreen.tsx",
  "Rights (/app/rights)": "src/app/RightsScreen.tsx",
  "Notices (/app/originals/$id/notices)": "src/app/NoticeDraftScreen.tsx",
  "AddOriginal (/app/originals/new)": "src/app/AddOriginalScreen.tsx",
};
// The words the first load pays for: the **core** slice of a language, loaded dynamically by
// `ensureI18nReady()` (src/i18n/index.ts), never through a static import. Count the larger
// language, so the budget holds for whichever one a visitor has.
//
// Issue #99: before the split this was the whole of `en.ts` / `de.ts` — 11.46 KB gzip of a
// 149.88 KB first load — although the overview names about a sixth of it. The four heavy screens'
// words are slices now (`src/i18n/slices.ts`), loaded by the route that shows them and reported
// below as what they add to that route.
const LANGUAGE_FILES = ["src/i18n/en/core.ts", "src/i18n/de/core.ts"];
// The composed resource: every slice in one module. Nothing shipped may import it — only the
// tests do (`src/i18n/resources.ts`) — because a static import of it would quietly put every
// screen's words back into some route's chunk and undo the split.
const WHOLE_RESOURCE_FILES = ["src/i18n/en.ts", "src/i18n/de.ts"];
// Which slice each screen's route loads, for the per-route figures. Kept in step with
// `src/i18n/slices.ts` by `src/app/translationKeys.test.ts`, which is what actually guards it.
const ROUTE_WORD_SLICES = {
  "CaseFile (/app/originals/$id/copies/$id)": "caseFile",
  "Rights (/app/rights)": "rights",
  "Notices list (/app/notices)": "notices",
  "Notices (/app/originals/$id/notices)": "notices",
  "AddOriginal (/app/originals/new)": "addOriginal",
};

function fail(message) {
  console.error(`\n✗ ${message}`);
  process.exitCode = 1;
}

if (!existsSync(MANIFEST_PATH)) {
  console.error(`No build manifest at ${path.relative(ROOT, MANIFEST_PATH)}. Run \`npm run build\` first.`);
  process.exit(1);
}

const manifest = JSON.parse(readFileSync(MANIFEST_PATH, "utf8"));

const gzipSizeCache = new Map();
function gzipSizeOf(chunkFile) {
  if (!gzipSizeCache.has(chunkFile)) {
    const bytes = readFileSync(path.join(DIST, chunkFile));
    gzipSizeCache.set(chunkFile, gzipSync(bytes, { level: 9 }).length);
  }
  return gzipSizeCache.get(chunkFile);
}

/** Every manifest key statically reachable from `key` (its own chunk plus `imports`, recursively). */
function staticClosure(key, seen = new Set()) {
  if (seen.has(key) || !manifest[key]) return seen;
  seen.add(key);
  for (const dep of manifest[key].imports ?? []) staticClosure(dep, seen);
  return seen;
}

function gzipKB(keys) {
  let bytes = 0;
  for (const key of keys) {
    const file = manifest[key]?.file;
    if (file && file.endsWith(".js")) bytes += gzipSizeOf(file);
  }
  return bytes / 1024;
}

function requireKey(key) {
  if (!manifest[key]) {
    fail(`"${key}" is not in the build manifest — did a route move without updating this script?`);
    return null;
  }
  return key;
}

const entryClosure = staticClosure("index.html");
const shellKey = requireKey(SHELL);
const shellClosure = shellKey ? staticClosure(shellKey) : new Set();
const sharedClosure = new Set([...entryClosure, ...shellClosure]);

const languageKB = Math.max(
  ...LANGUAGE_FILES.map((key) => (requireKey(key) ? gzipKB(staticClosure(key)) : 0)),
);

/** What a screen's word slice adds to its own route, in the larger language; 0 where it has none. */
function wordSliceKB(slice) {
  if (!slice) return 0;
  return Math.max(
    ...["en", "de"].map((language) => {
      const key = `src/i18n/${language}/${slice}.ts`;
      return manifest[key] ? gzipKB(staticClosure(key)) : 0;
    }),
  );
}

const entryKB = gzipKB(entryClosure);
const shellExtraKB = gzipKB([...shellClosure].filter((k) => !entryClosure.has(k)));

const firstLoadRouteKey = requireKey(FIRST_LOAD_ROUTE);
const firstLoadRouteExtraKB = firstLoadRouteKey
  ? gzipKB([...staticClosure(firstLoadRouteKey)].filter((k) => !sharedClosure.has(k)))
  : 0;

const firstLoadKB = entryKB + shellExtraKB + firstLoadRouteExtraKB + languageKB;

console.log("Customer app (/app) bundle budget — ADR 0028\n");
console.log("First load (fresh visit to /app, Home screen, larger language's core words):");
console.log(
  `  entry (React, ReactDOM, TanStack Router/Query core)   ${entryKB.toFixed(2)} KB gzip  (baseline ${ENTRY_BASELINE_KB} KB)`,
);
console.log(`  customer shell (CustomerApp, i18next, API client)     ${shellExtraKB.toFixed(2)} KB gzip`);
console.log(
  `  core words of the active language (larger of en/de)   ${languageKB.toFixed(2)} KB gzip  (budget ${CORE_WORDS_BUDGET_KB} KB)`,
);
console.log(`  Home route                                            ${firstLoadRouteExtraKB.toFixed(2)} KB gzip`);
console.log(`  ${"-".repeat(60)}`);
console.log(`  total                                                 ${firstLoadKB.toFixed(2)} KB gzip  (budget ${FIRST_LOAD_BUDGET_KB} KB)`);

// The two baselines behind that total (issue #99).
if (entryKB > ENTRY_BASELINE_KB) {
  fail(
    `the entry graph is ${entryKB.toFixed(2)} KB gzip, over its ${ENTRY_BASELINE_KB} KB baseline — ` +
      "something was added to what every visitor downloads before a screen of ours runs " +
      "(read the baseline note in this script before changing the number).",
  );
}
if (languageKB > CORE_WORDS_BUDGET_KB) {
  fail(
    `the core words are ${languageKB.toFixed(2)} KB gzip, over their ${CORE_WORDS_BUDGET_KB} KB budget — ` +
      "a screen's own words belong in that screen's slice (src/i18n/slices.ts), not in core.ts.",
  );
}
// The split only holds while nothing shipped imports the composed resource.
for (const key of WHOLE_RESOURCE_FILES) {
  if (!manifest[key]) continue;
  const shippedBy = [...sharedClosure, ...Object.values(OTHER_ROUTES).flatMap((r) => [...staticClosure(r)])];
  if (shippedBy.includes(key)) {
    fail(
      `${key} is in a shipped chunk: something imports the whole resource statically, which puts ` +
        "every screen's words back into the bundle. Import a slice, or `resources.ts` from a test.",
    );
  }
}

console.log(`\nAdditional routes (beyond the first load, ≤ ${ROUTE_BUDGET_KB} KB gzip each):`);
let worstRoute = 0;
for (const [label, key] of Object.entries(OTHER_ROUTES)) {
  const routeKey = requireKey(key);
  if (!routeKey) continue;
  const sliceKB = wordSliceKB(ROUTE_WORD_SLICES[label]);
  const extraKB =
    gzipKB([...staticClosure(routeKey)].filter((k) => !sharedClosure.has(k))) + sliceKB;
  worstRoute = Math.max(worstRoute, extraKB);
  const over = extraKB > ROUTE_BUDGET_KB ? "  OVER BUDGET" : "";
  const words = sliceKB > 0 ? `  (incl. ${sliceKB.toFixed(2)} KB of its own words)` : "";
  console.log(`  ${label.padEnd(42)} ${extraKB.toFixed(2).padStart(7)} KB gzip${words}${over}`);
  if (extraKB > ROUTE_BUDGET_KB) {
    fail(`${label} is ${extraKB.toFixed(2)} KB gzip, over the ${ROUTE_BUDGET_KB} KB per-route budget.`);
  }
}

if (firstLoadKB > FIRST_LOAD_BUDGET_KB) {
  fail(
    `/app's first load is ${firstLoadKB.toFixed(2)} KB gzip, over the ${FIRST_LOAD_BUDGET_KB} KB phase 1 budget ` +
      `(see the "Phase 1 numbers" comment in this script for why ADR 0028's own 130 KB is not yet reachable).`,
  );
}

if (process.exitCode) {
  console.error("\nBundle budget check failed.");
} else {
  console.log("\nBundle budget check passed.");
}
