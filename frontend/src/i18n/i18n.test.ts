/**
 * The seam's own test: a half-translated UI must fail the build, not ship.
 *
 * Two guarantees here, because each catches a different mistake:
 * 1. every language defines exactly the keys English defines (a forgotten translation), with no
 *    empty strings and no half-finished plural sets;
 * 2. every language uses the same interpolation placeholders per key (a dropped `{{count}}`,
 *    which renders a sentence with a hole in it).
 *
 * The third guarantee — that every key the customer UI *names* actually exists — lives next to the
 * screens it scans, in `src/app/translationKeys.test.ts`.
 */
import { describe, expect, it } from "vitest";

import { enAccountEmail } from "./en/accountEmail";
import { enAddOriginal } from "./en/addOriginal";
import { enCaseFile } from "./en/caseFile";
import { enCore } from "./en/core";
import { enDataRequests } from "./en/dataRequests";
import { enNotices } from "./en/notices";
import { enOnboarding } from "./en/onboarding";
import { enRights } from "./en/rights";
import { enVerification } from "./en/verification";
import { LANGUAGES } from "./language";
// Both languages, every slice merged (issue #99) — the resource a Next-app request holds and the
// one the SPA has once a screen's own slice is loaded.
import { customerEn, resources } from "./resources";

type Resources = typeof resources;
type LanguageCode = keyof Resources;

/** "signIn.errors.invalid" → the string, for every leaf in the tree. */
function flatten(value: unknown, prefix = ""): Map<string, string> {
  const flat = new Map<string, string>();
  if (typeof value === "string") {
    flat.set(prefix, value);
    return flat;
  }
  if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      const path = prefix ? `${prefix}.${key}` : key;
      for (const [childPath, childValue] of flatten(child, path)) flat.set(childPath, childValue);
    }
  }
  return flat;
}

function placeholders(text: string): string[] {
  return [...text.matchAll(/\{\{\s*([\w.]+)/g)].map((match) => match[1] ?? "").sort();
}

const REFERENCE: LanguageCode = "en";
const reference = flatten(customerEn);
const others = (Object.keys(resources) as LanguageCode[]).filter((code) => code !== REFERENCE);

describe("translation resources", () => {
  it("covers every language the switch offers", () => {
    expect(Object.keys(resources).sort()).toEqual([...LANGUAGES].sort());
  });

  it("has a non-empty English string for every key", () => {
    expect(reference.size).toBeGreaterThan(0);
    const empty = [...reference].filter(([, text]) => text.trim() === "").map(([key]) => key);
    expect(empty).toEqual([]);
  });

  it.each(others)("defines in %s exactly the keys English defines", (code) => {
    const translated = flatten(resources[code].translation);
    const missing = [...reference.keys()].filter((key) => !translated.has(key)).sort();
    const extra = [...translated.keys()].filter((key) => !reference.has(key)).sort();
    expect({ missing, extra }).toEqual({ missing: [], extra: [] });
  });

  it.each(others)("has no empty string in %s", (code) => {
    const translated = flatten(resources[code].translation);
    const empty = [...translated].filter(([, text]) => text.trim() === "").map(([key]) => key);
    expect(empty).toEqual([]);
  });

  it.each(others)("uses the same interpolation placeholders in %s as in English", (code) => {
    const translated = flatten(resources[code].translation);
    const mismatched = [...reference]
      .map(([key, text]) => ({
        key,
        en: placeholders(text),
        other: placeholders(translated.get(key) ?? ""),
      }))
      .filter((entry) => entry.en.join(",") !== entry.other.join(","));
    expect(mismatched).toEqual([]);
  });

  /**
   * The words are stored in slices so a screen's own strings cost only its route (issue #99).
   * `en.ts` merges them, and three sections are deliberately split across `core.ts` and a screen's
   * slice — so the one thing that could go wrong silently is a key lost in the merge, or defined
   * twice and quietly diverging between two modules. This is that check.
   */
  it("holds every key in exactly one slice", () => {
    const perSlice = [
      enCore,
      enCaseFile,
      enAddOriginal,
      enRights,
      enNotices,
      // Issue #115: the invite redemption's own slice. It is not in `slices.ts`, because only
      // the Next app has that screen — the SPA is the rollback, not the future (ADR 0052 §10).
      enOnboarding,
      // ADR 0055: showing us the platform account is yours. Its own slice for the same reason —
      // one screen shows it, and only the Next app has that screen.
      enVerification,
      // ADR 0056: asking for a copy of your data or for its deletion. Same reason again.
      enDataRequests,
      // Issue #149, ADR 0054: the mailed account actions' own slice, and not in `slices.ts` for the
      // same reason — only the Next app has those four screens.
      enAccountEmail,
    ].flatMap((slice) => [
      ...flatten(slice).keys(),
    ]);
    const twice = perSlice.filter((key, index) => perSlice.indexOf(key) !== index).sort();
    expect(twice).toEqual([]);
    expect([...new Set(perSlice)].sort()).toEqual([...reference.keys()].sort());
  });

  it.each(Object.keys(resources) as LanguageCode[])("has complete plural sets in %s", (code) => {
    const keys = [...flatten(resources[code].translation).keys()];
    const incomplete = keys
      .filter((key) => key.endsWith("_one"))
      .filter((key) => !keys.includes(`${key.slice(0, -"_one".length)}_other`));
    expect(incomplete).toEqual([]);
  });
});
