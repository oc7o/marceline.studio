#!/usr/bin/env node
/**
 * Bundle-size budget for the customer app (ADR 0028's performance table, ADR 0036).
 *
 * Reads the Next.js build output — `.next/build-manifest.json` for the chunks every page loads
 * from its `<head>`, and each route's `page_client-reference-manifest.js` for the chunks that
 * route's own client components need — so this measures the real dependency graph and
 * **discovers the routes itself**. A new screen is measured the moment it is built; there is no
 * route list to keep in step. Run after `npm run build`.
 *
 * What a browser downloads for one route:
 *
 *   the shared entry (webpack runtime, React, the App Router client runtime, main-app)
 *   + the stylesheet
 *   + that route's own client chunks (the shell's islands, the layout, the page)
 *
 * The polyfill chunk is **excluded**: Next emits it with `noModule`, so every browser that can run
 * the app skips it. It is counted and printed separately, so the exclusion is visible rather than
 * assumed.
 *
 * **Upper bound by about half a kilobyte.** A route's client-reference manifest lists a couple of
 * sibling chunks the runtime does not actually fetch (`app/layout`, ~0.17 KB, and the overview's
 * `(signed-in)/page`, ~0.35 KB). Measured against the served HTML of the real app on 2026-09-17,
 * this script reports 0.5 KB more than a browser downloads, never less — which is the safe
 * direction for a budget. `make customer-smoke` serves the production image and is where the live
 * figure is checked.
 *
 * ## The budget, and its headroom
 *
 * ADR 0028 (d) asks for ≤ 130 KB gzip first load. That is reachable only on Next 15.5, whose
 * client runtime is ~27 KB smaller; the operator chose the **current major** instead (16.3), for
 * the RSC security releases, knowing the cost. ADR 0036 records both measurements.
 *
 * `FIRST_LOAD_BUDGET_KB` is therefore **the measured worst route plus 4 KB**: enough for a screen
 * of ours to grow by a form or a table without a budget failure, and small enough that adding a
 * *library* to the shared graph cannot pass unnoticed. It is not a round number on purpose — the
 * number's job is to fail on a regression, not to look tidy. Raising it is a decision, and the
 * comment it needs is why the new bytes are worth having.
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { gzipSync } from "node:zlib";

const ROOT = path.dirname(import.meta.dirname);
const DIST = path.join(ROOT, ".next");

/**
 * Measured worst route (add-original, 145.7 KB on Next 16.3 + webpack) plus 4 KB of headroom.
 *
 * Raised from 147 on 2026-09-18, when the design system's tokens landed (`app/globals.css`): the
 * stylesheet went from 4.4 KB to 6.4 KB gzip — the colour tokens, in both themes, plus the type
 * scale — and every route's first load carries the stylesheet. The bytes are a **one-time** cost
 * for the whole token layer, not a library: no dependency was added, and the shared entry did not
 * move (127.94 KB before and after). Stylesheet growth from here has its own guard, `CSS_BUDGET_KB`
 * below, which this uses 6.4 KB of 15.
 *
 * Fonts are **not** in this number and must never be smuggled into it: they are not JavaScript, a
 * browser fetches them in parallel with it, and `font-display: swap` means they block no paint.
 * They have their own budget, `FONT_BUDGET_KB` below.
 */
const FIRST_LOAD_BUDGET_KB = 150;
// **Per-route budget: 16 KB, not ADR 0028's 30** (operator decision 2026-09-18, issue #99). The
// per-route number is the only one of these that measures our own code: the first-load total is
// dominated by the framework floor, so it cannot fail on a screen doubling in size. Our worst route
// spends 11.55 KB (the Next app) and 10.71 KB (this SPA) of its own, and none has ever been near 30,
// so 30 guarded nothing. 16 is the worst route plus ~4.5 KB — about what a screen grows by when it
// gains a form or a table — and the totals stay at 150.
const ROUTE_BUDGET_KB = 16;
const CSS_BUDGET_KB = 15;

/**
 * The shared entry on its own — the framework floor (issue #99).
 *
 * Measured 127.56 KB gzip on 2026-09-18 against ADR 0036's 127.4 KB hello-world on the same major:
 * **almost the whole first-load number is Next's own client runtime and React**, and our code is
 * 5.31 KB on a plain route. That makes the first-load total a poor alarm on its own — trimming a
 * screen by 2 KB silently makes room for 2 KB of framework or library in the shared graph, which
 * is the more expensive kind of growth because every route pays it.
 *
 * So this is guarded separately, at the measured number plus ~1 KB. It fails on a Next upgrade
 * that costs bytes, on a `"use client"` module hoisted into the shared entry, and on a dependency
 * added to it — none of which the per-route budget below would catch on its own. Raising it is a
 * decision; the comment it needs is what the new bytes buy.
 */
const SHARED_ENTRY_BUDGET_KB = 128.5;

/**
 * The self-hosted typefaces (`public/fonts/`, declared in `app/globals.css`), in **raw bytes**:
 * woff2 is brotli-compressed already, so gzipping it again measures nothing.
 *
 * Measured 2026-09-18, subset from the operator's Google Fonts TTFs with fontTools 4.65 — every
 * file, and the subtotal a reader actually downloads:
 *
 *   Instrument Sans (variable 400-700)  latin 32.4 KB   latin-ext 11.4 KB
 *   Instrument Serif (400)              latin 20.9 KB   latin-ext 11.6 KB
 *   JetBrains Mono (variable 400-700)   latin 14.5 KB   latin-ext 10.4 KB
 *                                       ----------------------------------
 *                                       total on disk 101.4 KB
 *                                       an English or German page: 67.9 KB, the three latin files,
 *                                       and only those of them a screen renders a glyph from
 *
 * The latin-ext halves are fetched only when a name carries a character from that range — a Polish
 * or Turkish creator name — so they cost nothing on an ordinary page and are the difference between
 * that name rendering in our type and rendering in the fallback.
 *
 * `FONT_BUDGET_KB` is the measured 101.4 KB plus ~8.6 KB. The headroom is deliberately **less than
 * one family**: a fourth typeface (30-45 KB subset) cannot be added without this failing, which is
 * the whole point of the line. Growth within a family — an italic, a second weight range — is a
 * decision too; take the failure and write down why the bytes are worth having.
 */
const FONT_BUDGET_KB = 110;

function fail(message) {
  console.error(`\n✗ ${message}`);
  process.exitCode = 1;
}

const BUILD_MANIFEST = path.join(DIST, "build-manifest.json");
if (!existsSync(BUILD_MANIFEST)) {
  console.error("No .next/build-manifest.json. Run `npm run build` first.");
  process.exit(1);
}
const buildManifest = JSON.parse(readFileSync(BUILD_MANIFEST, "utf8"));

const gzipCache = new Map();
function gzipBytes(file) {
  if (!gzipCache.has(file)) {
    // Chunk paths for dynamic segments are URL-encoded in the manifests (`%5BoriginalId%5D`); the
    // file on disk is not.
    const onDisk = path.join(DIST, decodeURIComponent(file));
    gzipCache.set(file, gzipSync(readFileSync(onDisk), { level: 9 }).length);
  }
  return gzipCache.get(file);
}

const kb = (files) => {
  let bytes = 0;
  for (const file of new Set(files)) bytes += gzipBytes(file);
  return bytes / 1024;
};

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
}

/**
 * Every route's client chunks and stylesheets, from the per-route manifests Next writes beside
 * each compiled page. They assign into `globalThis.__RSC_MANIFEST`, keyed by the route.
 */
function routeManifests() {
  const appDir = path.join(DIST, "server", "app");
  if (!existsSync(appDir)) {
    console.error("No .next/server/app. Run `npm run build` first.");
    process.exit(1);
  }
  globalThis.__RSC_MANIFEST = {};
  for (const file of walk(appDir)) {
    // Only pages: an `/api/*` route handler ships no client chunk a browser could download.
    if (!file.endsWith("page_client-reference-manifest.js")) continue;
    const source = readFileSync(file, "utf8");
    // The file is `globalThis.__RSC_MANIFEST[...]={...}` — evaluated, not parsed, because the
    // assignment is what names the route.
    new Function(source)();
  }
  return globalThis.__RSC_MANIFEST;
}

/** The URL a customer types, from a manifest key: route groups drop out, `basePath` goes on. */
function urlOf(routeKey) {
  const segments = routeKey
    .replace(/\/page$/, "")
    .split("/")
    .filter((segment) => segment !== "" && !segment.startsWith("("));
  return `/app${segments.length ? `/${segments.join("/")}` : ""}`;
}

const manifests = routeManifests();
const routeKeys = Object.keys(manifests)
  .filter((key) => key.endsWith("/page") && !key.startsWith("/_"))
  .sort();

if (routeKeys.length === 0) {
  fail("no /app routes in the build output — did the build produce any pages?");
}

const sharedFiles = (buildManifest.rootMainFiles ?? []).filter((file) => file.endsWith(".js"));
const sharedKB = kb(sharedFiles);
const polyfillKB = kb((buildManifest.polyfillFiles ?? []).filter((file) => file.endsWith(".js")));

function chunksOf(routeKey) {
  const manifest = manifests[routeKey];
  const js = new Set();
  for (const entry of Object.values(manifest.clientModules ?? {})) {
    for (const chunk of entry.chunks ?? []) if (String(chunk).endsWith(".js")) js.add(String(chunk));
  }
  const css = new Set();
  for (const list of Object.values(manifest.entryCSSFiles ?? {})) {
    for (const entry of list) {
      const file = typeof entry === "string" ? entry : entry.path;
      if (file && !entry.inlined) css.add(file);
    }
  }
  return { js: [...js], css: [...css] };
}

const cssKB = kb(routeKeys.flatMap((key) => chunksOf(key).css));

console.log("Customer app (Next.js 16.3, webpack) bundle budget — ADR 0028 (d), ADR 0036\n");
console.log(
  `shared entry (React + App Router client runtime)   ${sharedKB.toFixed(2).padStart(7)} KB gzip  (baseline ${SHARED_ENTRY_BUDGET_KB} KB)`,
);
if (sharedKB > SHARED_ENTRY_BUDGET_KB) {
  fail(
    `the shared entry is ${sharedKB.toFixed(2)} KB gzip, over its ${SHARED_ENTRY_BUDGET_KB} KB baseline — ` +
      "the framework floor every route pays grew (read the baseline note at the top of this script).",
  );
}
console.log(
  `legacy polyfills (noModule — modern browsers skip) ${polyfillKB.toFixed(2).padStart(7)} KB gzip  (not counted)`,
);
console.log(
  `stylesheet                                        ${cssKB.toFixed(2).padStart(7)} KB gzip  (budget ${CSS_BUDGET_KB} KB)`,
);
if (cssKB > CSS_BUDGET_KB) {
  fail(`the stylesheet is ${cssKB.toFixed(2)} KB gzip, over the ${CSS_BUDGET_KB} KB budget.`);
}

console.log("\nPer route (first load = shared entry + stylesheet + the route's own chunks):\n");
console.log(`  ${"route".padEnd(46)} ${"first load".padStart(12)} ${"own chunks".padStart(12)}`);
for (const key of routeKeys) {
  const { js, css } = chunksOf(key);
  const ownKB = kb(js.filter((file) => !sharedFiles.includes(file)));
  const firstLoadKB = sharedKB + kb(css) + ownKB;
  const flags = [
    firstLoadKB > FIRST_LOAD_BUDGET_KB ? "OVER FIRST LOAD" : "",
    ownKB > ROUTE_BUDGET_KB ? "OVER ROUTE" : "",
  ]
    .filter(Boolean)
    .join(" ");
  console.log(
    `  ${urlOf(key).padEnd(46)} ${`${firstLoadKB.toFixed(2)} KB`.padStart(12)} ${`${ownKB.toFixed(2)} KB`.padStart(12)}  ${flags}`,
  );
  if (firstLoadKB > FIRST_LOAD_BUDGET_KB) {
    fail(
      `${urlOf(key)} first-loads ${firstLoadKB.toFixed(2)} KB gzip, over the ${FIRST_LOAD_BUDGET_KB} KB budget ` +
        "(read the budget note at the top of this script before changing the number).",
    );
  }
  if (ownKB > ROUTE_BUDGET_KB) {
    fail(
      `${urlOf(key)} adds ${ownKB.toFixed(2)} KB gzip of its own, over the ${ROUTE_BUDGET_KB} KB per-route budget.`,
    );
  }
}

/*
  The fonts, measured on disk rather than in the build output: they are static files under
  `public/`, so nothing in `.next` mentions them. `app/tokens.test.ts` is what checks that every
  file here is actually referenced by the stylesheet and vice versa; this only weighs them.
*/
const FONT_DIR = path.join(ROOT, "public", "fonts");
if (!existsSync(FONT_DIR)) {
  fail("no public/fonts — the self-hosted typefaces are missing (app/globals.css references them).");
} else {
  const fonts = readdirSync(FONT_DIR)
    .filter((name) => name.endsWith(".woff2"))
    .sort();
  console.log("\nSelf-hosted typefaces (raw bytes — woff2 is already brotli):\n");
  let totalKB = 0;
  let firstViewKB = 0;
  for (const name of fonts) {
    const fileKB = statSync(path.join(FONT_DIR, name)).size / 1024;
    totalKB += fileKB;
    // What an English or German page asks for: the latin halves. The latin-ext halves wait for a
    // character that needs them. Filenames carry an 8-hex content hash (issue #84), so this
    // matches `…-latin.<hash>.woff2` without also matching `…-latin-ext.<hash>.woff2`.
    if (/-latin\.[0-9a-f]{8}\.woff2$/.test(name)) firstViewKB += fileKB;
    console.log(`  ${name.padEnd(46)} ${`${fileKB.toFixed(2)} KB`.padStart(12)}`);
  }
  console.log(
    `  ${"total on disk".padEnd(46)} ${`${totalKB.toFixed(2)} KB`.padStart(12)}  (budget ${FONT_BUDGET_KB} KB)`,
  );
  console.log(
    `  ${"an English or German page (latin only)".padEnd(46)} ${`${firstViewKB.toFixed(2)} KB`.padStart(12)}`,
  );
  if (totalKB > FONT_BUDGET_KB) {
    fail(
      `the typefaces total ${totalKB.toFixed(2)} KB, over the ${FONT_BUDGET_KB} KB font budget ` +
        "(read the font budget note at the top of this script before changing the number).",
    );
  }
}

console.log(
  `\n  budgets: first load ${FIRST_LOAD_BUDGET_KB} KB · per route ${ROUTE_BUDGET_KB} KB · ` +
    `CSS ${CSS_BUDGET_KB} KB (gzip) · fonts ${FONT_BUDGET_KB} KB (raw)`,
);

if (process.exitCode) console.error("\nBundle budget check failed.");
else console.log("\nBundle budget check passed.");
