/**
 * The fingerprinting code must never load with the app (issue #29, ADR 0036).
 *
 * `fingerprintClient`, its Worker and the PDQ WASM together are about 26 KB gzip. They belong to
 * one button on one screen, and a static import anywhere would put them into that screen's chunk
 * — or, through a shared module, into every visitor's first load. The Vite SPA has the same test
 * over `src/`; this is its half for the Next app.
 *
 * Use `import type` for the types, and `@shared/lib/fingerprintProgress` /
 * `@shared/lib/videoFingerprint` for the pure helpers.
 */
import { describe, expect, it } from "vitest";

import { sourceFiles } from "./sourceFiles";

const HEAVY = /(fingerprintClient|fingerprintWorker|wasm\/pdq-wasm|lib\/pdq)/;
/** A static `import … from "…"` (not `import(` and not `import type`). */
const STATIC_IMPORT = /^\s*import\s+(?!type\b)[^;]*?from\s+["']([^"']+)["']/gm;

const sources = sourceFiles();

describe("the fingerprinting code is loaded on demand", () => {
  it("found the sources", () => {
    expect(sources.length).toBeGreaterThan(10);
  });

  it.each(sources.map(({ file, source }) => [file, source] as const))(
    "%s does not statically import it",
    (_file, source) => {
      const offenders = [...source.matchAll(STATIC_IMPORT)]
        .map((match) => match[1] ?? "")
        .filter((specifier) => HEAVY.test(specifier));
      expect(offenders).toEqual([]);
    },
  );

  it("is reached by import() from the one screen that needs it", () => {
    const screen = sources.find(({ file }) => file.endsWith("AddOriginalScreen.tsx"));
    expect(screen).toBeDefined();
    expect(screen?.source).toMatch(/await import\("@shared\/lib\/fingerprintClient"\)/);
  });
});
