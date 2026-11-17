/**
 * The design system's tokens, checked against `src/index.css` itself.
 *
 * The customer app states the same rules for its own stylesheet (`frontend/customer/app/
 * tokens.test.ts`); this is the staff SPA's copy, and it differs in exactly the places the two
 * surfaces differ:
 *
 * 1. **Every colour token has both themes.** A token declared in `@theme` and forgotten in the dark
 *    block keeps its light value on a dark ground — which is how a 4.5:1 floor is lost silently, in
 *    the one state nobody looked at.
 * 2. **`blur-thumb` must be here.** The customer app forbids it (it shows no copy of the material,
 *    issue #53). This app is the surface the system's media rule was written for: unreviewed media
 *    sits blurred until a reviewer asks for it, so the token's *presence* is the product decision.
 * 3. **No screen reaches past the tokens.** A raw Tailwind palette class has one value in both
 *    themes, which is how half a screen stays light on a dark ground.
 * 4. **This stylesheet loads nothing at all.** The customer app self-hosts three woff2 families and
 *    permits `url()` into its own `public/fonts/`; this one ships no font files (the reasons are in
 *    `index.css`), so the strictest possible form of ADR 0028's rule applies — not one `url()`.
 */
/// <reference types="node" />
import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

/*
  Read with `fs`, not with `?raw`: Tailwind's Vite plugin claims every `.css` import and hands a
  `?raw` one back empty. The node types are pulled in for this file alone by the reference above —
  `tsconfig.app.json` keeps them out of the app, where `fs` has no business.
*/
const css = readFileSync(path.join(import.meta.dirname, "index.css"), "utf8");

/** The stylesheet without its comments — the prose explains the rules and must not trip them. */
const code = css.replace(/\/\*[\s\S]*?\*\//g, "");

/** The `@theme` block, where the light values and the utility names are declared. */
const theme = /@theme\s*\{([\s\S]*?)\n\}/.exec(css)?.[1] ?? "";
const dark = css.slice(css.indexOf("@media (prefers-color-scheme: dark)"));

function colourNames(source: string): string[] {
  return [...source.matchAll(/^\s*(--color-[a-z0-9-]+):/gm)].map((match) => match[1] ?? "").sort();
}

/**
 * Tokens that are the same value in both themes, on purpose: `on-brand` is the warm white that
 * keeps either red from vibrating, and the three `critical` names are aliases of `brand` — they
 * follow it, so overriding them would be a second opinion.
 */
const THEME_INDEPENDENT = [
  "--color-critical",
  "--color-critical-ink",
  "--color-critical-quiet",
  "--color-on-brand",
];

describe("the token set", () => {
  it("declares the light theme in @theme", () => {
    expect(colourNames(theme).length).toBeGreaterThan(20);
  });

  it("gives every colour token a dark value", () => {
    const missing = colourNames(theme)
      .filter((name) => !THEME_INDEPENDENT.includes(name))
      .filter((name) => !dark.includes(`${name}:`));
    expect(missing).toEqual([]);
  });

  it("overrides the dark theme twice: the reader's setting, and an explicit choice", () => {
    expect(css).toContain(':root:not([data-theme="light"])');
    expect(css).toContain(':root[data-theme="dark"]');
    const media = colourNames(
      css.slice(css.indexOf("@media (prefers-color-scheme: dark)"), css.indexOf(':root[data-theme="dark"]')),
    );
    const explicit = colourNames(css.slice(css.indexOf(':root[data-theme="dark"]')));
    expect(explicit).toEqual(media);
  });

  it("gives the body an explicit ground and ink", () => {
    expect(css).toMatch(/body\s*\{[^}]*background-color:\s*var\(--color-surface-000\)/);
    expect(css).toMatch(/body\s*\{[^}]*color:\s*var\(--color-ink\)/);
  });

  it("states the focus ring once, for everything focusable", () => {
    expect(css).toMatch(/:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--color-focus\)/);
    expect(css).toMatch(/:focus-visible\s*\{[^}]*outline-offset:\s*2px/);
  });

  it("respects prefers-reduced-motion", () => {
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
  });

  it("declares blur-thumb: this is the app that shows unreviewed media", () => {
    expect(css).toMatch(/--blur-thumb:\s*\d+px/);
  });
});

/*
  ADR 0028's rule is **no auto-loaded external resource**. The customer app can only satisfy it by
  policing where its `url()`s point; this app satisfies it by having none, which is the version that
  cannot be got wrong. Nothing below may be loosened to admit a host — a webfont request carries the
  reader's IP and `Referer` to whoever answers it, on every page load.
*/
describe("the stylesheet loads nothing", () => {
  it("names no host, in any form", () => {
    expect(code).not.toMatch(/https?:\/\//);
    expect(code).not.toMatch(/@import\s+url\(/);
  });

  it("contains no url() and no @font-face at all", () => {
    expect(code).not.toMatch(/url\(/);
    expect(code).not.toMatch(/@font-face/);
  });
});

/**
 * Tailwind's own palette, which no staff screen may reach for. `components/ui.tsx` and the screens
 * draw from `src/index.css`; a literal here has one value in both themes, so a dark-set machine
 * gets slate-on-black or white-on-white and the theme cannot reach it.
 *
 * `src/app/**` is out of scope by construction (see `STAFF_SOURCES`): it is the customer app's
 * rollback copy, pinned to a light ground and on its way out with ADR 0036. `src/wasm/**` and
 * `src/api/schema.d.ts` are generated and carry no class name.
 */
const LITERAL_COLOUR =
  /\b(?:bg|text|border|from|via|to|ring|outline|divide|decoration|placeholder|accent|caret|shadow|fill|stroke)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}\b|\b(?:bg|text|border|divide|ring)-(?:white|black)\b/;

/**
 * Every staff `.ts`/`.tsx` file, inlined by Vite at transform time (the same device
 * `lazyFingerprinting.test.ts` uses): no `fs`, and the scan covers the files no test ever renders.
 * Tests are excluded — this one names the forbidden construct in order to forbid it.
 */
const STAFF_SOURCES = import.meta.glob<string>(
  [
    "./components/**/*.ts",
    "./components/**/*.tsx",
    "./screens/**/*.ts",
    "./screens/**/*.tsx",
    "./lib/**/*.ts",
    "./lib/**/*.tsx",
    "./hooks/**/*.ts",
    "./hooks/**/*.tsx",
    "./router.tsx",
    "./main.tsx",
    "!./**/*.test.ts",
    "!./**/*.test.tsx",
    "!./**/*.d.ts",
  ],
  { eager: true, query: "?raw", import: "default" },
);

describe("the staff screens are on the tokens", () => {
  const sources = Object.entries(STAFF_SOURCES)
    .map(([file, source]) => ({ file: file.replace(/^\.\//, ""), source }))
    .sort((a, b) => a.file.localeCompare(b.file));

  it("has staff sources to check", () => {
    expect(sources.length).toBeGreaterThan(10);
  });

  it.each(sources.map(({ file }) => file))("%s names no literal colour", (file) => {
    const source = sources.find((entry) => entry.file === file)?.source ?? "";
    const hit = LITERAL_COLOUR.exec(source);
    expect(hit?.[0] ?? null).toBeNull();
  });
});
