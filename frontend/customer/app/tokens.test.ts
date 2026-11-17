/**
 * The design system's tokens, checked against `globals.css` itself.
 *
 * Four things a reviewer would otherwise have to hold in their head:
 *
 * 1. **Every colour token has both themes.** A token declared in `@theme` and forgotten in the dark
 *    block keeps its light value on a dark ground — which is how a 4.5:1 floor is lost silently,
 *    in the one state nobody looked at.
 * 2. **`blur-thumb` never comes back.** It is the system's rule for unreviewed media, and this app
 *    shows none (issue #53). Its absence is a product decision, not an oversight.
 * 3. **No screen reaches past the tokens.** A raw Tailwind palette class has one value in both
 *    themes, which is how half a screen stays light on a dark ground.
 * 4. **Nothing in the stylesheet reaches off our origin.** The three typefaces are self-hosted
 *    woff2 files under `public/fonts/`; the last block below is what keeps them that way.
 */
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { sourceFiles } from "../lib/sourceFiles";

const css = readFileSync(path.join(import.meta.dirname, "globals.css"), "utf8");

/**
 * `<name>.<8-hex sha256>.woff2` (issue #84): the hash is what makes the year-long, `immutable`
 * `Cache-Control` on `/app/fonts/*` (`next.config.ts`) honest — a changed file gets a new URL. Kept
 * as strict as the old no-hash shape it replaces: still only our own path, still only a woff2.
 */
const FONT_URL = /^\/app\/fonts\/[a-z0-9-]+\.[0-9a-f]{8}\.woff2$/;

/** The `@theme` block, where the light values and the utility names are declared. */
const theme = /@theme\s*\{([\s\S]*?)\n\}/.exec(css)?.[1] ?? "";
const dark = css.slice(css.indexOf("@media (prefers-color-scheme: dark)"));

function colourNames(source: string): string[] {
  return [...source.matchAll(/^\s*(--color-[a-z0-9-]+):/gm)].map((match) => match[1] ?? "").sort();
}

/**
 * Tokens that are the same value in both themes, on purpose:
 * `on-brand` is the warm white that keeps either red from vibrating, and the three `critical`
 * names are aliases of `brand` — they follow it, so overriding them would be a second opinion.
 */
const THEME_INDEPENDENT = ["--color-critical", "--color-critical-ink", "--color-critical-quiet", "--color-on-brand"];

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
    const media = colourNames(css.slice(css.indexOf('@media (prefers-color-scheme: dark)'), css.indexOf(':root[data-theme="dark"]')));
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

  it("declares no blur-thumb: this app shows no media to blur (#53)", () => {
    expect(css).not.toMatch(/--blur-thumb\s*:/);
  });

  /*
    The rule being protected is ADR 0028's — **no auto-loaded external resource** — not "no fonts".
    Until 2026-09-18 this file forbade `@font-face` outright, because there were no font files and
    the only way one could have appeared was someone reaching for Google Fonts. The fonts are ours
    now and self-hosted, so the ban moves to where the harm is: any URL that leaves our origin.
    Nothing below may be loosened to admit a host — a webfont request carries the reader's IP and
    `Referer` to whoever answers it, on every page load, for every customer.
  */
  describe("every resource it loads is our own", () => {
    it("names no external host, in any form", () => {
      expect(css).not.toMatch(/https?:\/\//);
      // `//fonts.example/…` inherits the page's scheme and is just as external.
      expect(css).not.toMatch(/url\(\s*["']?\s*\/\//);
      expect(css).not.toMatch(/@import\s+url\(/);
    });

    it("points every url() at a file of ours, by absolute path", () => {
      const urls = [...css.matchAll(/url\(\s*["']?([^"')]+)["']?\s*\)/g)].map((m) => m[1] ?? "");
      for (const url of urls) {
        expect(url).toMatch(FONT_URL);
      }
    });

    it("declares @font-face only for self-hosted woff2, with a swap fallback", () => {
      const faces = [...css.matchAll(/@font-face\s*\{([\s\S]*?)\n\}/g)].map((m) => m[1] ?? "");
      expect(faces.length).toBeGreaterThan(0);
      for (const face of faces) {
        const urls = [...face.matchAll(/url\(\s*["']([^"']+)["']\s*\)/g)].map((m) => m[1] ?? "");
        expect(urls).not.toEqual([]);
        for (const url of urls) expect(url).toMatch(FONT_URL);
        // A face without `swap` blocks the text it is for; ours never do.
        expect(face).toMatch(/font-display:\s*swap;/);
      }
    });

    /*
      The path is `/app/fonts/…`, not `/fonts/…`: `basePath` rewrites `next/link`, never a `url()`
      in a stylesheet, and production serves exactly `/app` and `/app/*` (ADR 0028). Getting this
      wrong works in development and 404s in production, so the files are checked where they will
      actually be looked for.
    */
    it("ships every file it references, and references every file it ships", () => {
      const dir = path.join(import.meta.dirname, "..", "public", "fonts");
      const referenced = new Set(
        [...css.matchAll(/url\(\s*["']\/app\/fonts\/([^"']+)["']\s*\)/g)].map((m) => m[1] ?? ""),
      );
      const shipped = new Set(readdirSync(dir).filter((name) => name.endsWith(".woff2")));
      expect([...referenced].filter((name) => !shipped.has(name))).toEqual([]);
      expect([...shipped].filter((name) => !referenced.has(name))).toEqual([]);
    });

    /*
      The whole point of hashing the filename (issue #84) is that `immutable` never lies: a file
      whose bytes changed must get a new URL. This is the guard — it recomputes each shipped file's
      sha256 and fails if the 8 hex characters in its name are not a prefix of the real digest, the
      exact failure mode a hand-edited font file in place (rather than a renamed one) would produce.
    */
    it("names every shipped file by a hash of its own contents", () => {
      const dir = path.join(import.meta.dirname, "..", "public", "fonts");
      const shipped = readdirSync(dir).filter((name) => name.endsWith(".woff2"));
      for (const name of shipped) {
        const match = /^(.+)\.([0-9a-f]{8})\.woff2$/.exec(name);
        expect(match, `${name} does not have the <name>.<8-hex hash>.woff2 shape`).not.toBeNull();
        const [, , hash] = match ?? [];
        const digest = createHash("sha256").update(readFileSync(path.join(dir, name))).digest("hex");
        expect(digest.slice(0, 8), `${name}'s hash does not match its contents`).toEqual(hash);
      }
    });

    it("keeps the OFL notice with the files it covers", () => {
      const notice = readFileSync(
        path.join(import.meta.dirname, "..", "public", "fonts", "LICENSES.txt"),
        "utf8",
      );
      for (const family of ["Instrument Sans", "Instrument Serif", "JetBrains Mono"]) {
        expect(notice).toContain(family);
      }
      expect(notice).toContain("SIL Open Font License, Version 1.1");
    });
  });
});

/**
 * Tailwind's own palette, which no screen of this app may reach for. A literal has one value in
 * both themes, so a dark-set OS gets slate-on-black or, worse, white-on-white: the theme cannot
 * reach it. `components/ui.test.tsx` states the same rule for the primitives by rendering them;
 * this states it for every file, including the ones a test never renders.
 */
const LITERAL_COLOUR =
  /\b(?:bg|text|border|from|via|to|ring|outline|divide|decoration|placeholder|accent|caret|shadow|fill|stroke)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}\b|\b(?:bg|text|border|divide|ring)-(?:white|black)\b/;

describe("the screens are on the tokens", () => {
  const sources = sourceFiles();

  it("has customer sources to check", () => {
    expect(sources.length).toBeGreaterThan(10);
  });

  it.each(sources.map(({ file }) => file))("%s names no literal colour", (file) => {
    const source = sources.find((entry) => entry.file === file)?.source ?? "";
    const hit = LITERAL_COLOUR.exec(source);
    expect(hit?.[0] ?? null).toBeNull();
  });
});
