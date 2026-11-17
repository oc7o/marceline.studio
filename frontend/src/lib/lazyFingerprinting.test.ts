/**
 * The fingerprinting code (client, Worker, PDQ WASM) stays out of the app's main chunk: only the
 * pages that fingerprint load it, and only on demand (issue #29, performance is an acceptance
 * criterion). A static import anywhere else would silently pull it into every page load, so this
 * test reads the source and allows type-only imports, the modules' own files and tests.
 */
import { describe, expect, it } from "vitest";

const sources = import.meta.glob<string>(["../**/*.ts", "../**/*.tsx", "!../**/*.test.ts", "!../**/*.test.tsx"], {
  eager: true,
  query: "?raw",
  import: "default",
});

/** Modules that are the fingerprinting code itself, and may import each other. */
const FINGERPRINTING = ["/fingerprintClient.ts", "/fingerprintWorker.ts", "/pdq.ts"];
const STATIC_IMPORT = /^\s*import\s+(?!type\b)[^;]*?from\s+"[^"]*\/(fingerprintClient|fingerprintWorker|pdq)"/gm;

describe("fingerprinting code is loaded lazily", () => {
  it("has sources to check", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(20);
  });

  it("is never imported statically outside itself", () => {
    const offenders = Object.entries(sources)
      .filter(([file]) => !FINGERPRINTING.some((own) => file.endsWith(own)))
      .flatMap(([file, source]) => [...source.matchAll(STATIC_IMPORT)].map((match) => `${file}: ${match[0].trim()}`));
    expect(offenders).toEqual([]);
  });

  it("reaches the customer page through a lazy route", () => {
    const router = Object.entries(sources).find(([file]) => file.endsWith("/router.tsx"))?.[1] ?? "";
    expect(router).toMatch(/lazyRouteComponent\(\s*\(\)\s*=>\s*import\("\.\/app\/AddOriginalScreen"\)/);
    expect(router).not.toMatch(/import\s*\{[^}]*AddOriginalScreen[^}]*\}\s*from/);
  });
});
