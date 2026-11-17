/**
 * Every translation key the customer UI names must exist in the resources.
 *
 * TypeScript cannot see inside a string, so a typo would otherwise render the key itself to a
 * paying customer. This check lives next to the screens it scans; the resources themselves — same
 * keys, same placeholders, both languages — are guarded by `src/i18n/i18n.test.ts`.
 */
import { describe, expect, it } from "vitest";

// The merged resource: every slice together, which is what the Next app has at request time.
import { enAddOriginal } from "../i18n/en/addOriginal";
import { enCaseFile } from "../i18n/en/caseFile";
import { enCore } from "../i18n/en/core";
import { enNotices } from "../i18n/en/notices";
import { enRights } from "../i18n/en/rights";
import { customerEn } from "../i18n/resources";
import { SCREEN_WORDS, type Slice } from "../i18n/slices";

/** Every key path in the resource tree, as "signIn.errors.invalid". */
function flatten(value: unknown, prefix = ""): string[] {
  if (typeof value === "string") return [prefix];
  if (!value || typeof value !== "object") return [];
  return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) =>
    flatten(child, prefix ? `${prefix}.${key}` : key),
  );
}

// The customer UI's own source, tests excluded: a test naming a bad key would otherwise fail on
// its own text. Both customer apps are scanned while both exist (ADR 0036): this SPA's `/app`
// screens, and the Next.js app in `frontend/customer/`, which shares these very resources.
const sources = {
  ...import.meta.glob<string>(
    ["./**/*.ts", "./**/*.tsx", "!./**/*.test.ts", "!./**/*.test.tsx"],
    { eager: true, query: "?raw", import: "default" },
  ),
  ...import.meta.glob<string>(
    [
      "../../customer/app/**/*.{ts,tsx}",
      "../../customer/components/**/*.{ts,tsx}",
      "../../customer/screens/**/*.{ts,tsx}",
      "../../customer/lib/**/*.{ts,tsx}",
      "!../../customer/**/*.test.{ts,tsx}",
    ],
    { eager: true, query: "?raw", import: "default" },
  ),
};

// `t("…")` in a component, and `key: "…"` in the pure message mappers (signInError.ts), which name
// keys without calling `t` themselves.
const KEY_PATTERNS = [/\bt\(\s*"([^"]+)"/g, /\bkey:\s*"([^"]+)"/g];
// The static head of a template-literal key, e.g. `t(`rights.works.blockers.${code}`)`.
const PREFIX_PATTERN = /\bt\(\s*`([A-Za-z][\w.]*\.)/g;

/**
 * Which slice of the words each SPA file may name (issue #99). Screens come from `slices.ts`, the
 * one place the router loads by, so a route and its test cannot drift apart; the only other entry
 * is the add-original screen's pure failure mapper, which that screen alone imports. Every file not
 * listed may name the core words only — that is what keeps a shared part (`parts.tsx`) from naming
 * a key that exists on one screen and not on another.
 */
const FILE_SLICES: Record<string, readonly Slice[]> = {
  ...Object.fromEntries(
    Object.entries(SCREEN_WORDS).map(([screen, slices]) => [`./${screen}.tsx`, slices]),
  ),
  "./addOriginal.ts": ["addOriginal"],
  "./NoticeSubmissionsBlock.tsx": ["notices"],
};

const SLICE_WORDS: Record<Slice, unknown> = {
  caseFile: enCaseFile,
  addOriginal: enAddOriginal,
  rights: enRights,
  notices: enNotices,
};

const used = Object.entries(sources).flatMap(([file, source]) =>
  KEY_PATTERNS.flatMap((pattern) =>
    [...source.matchAll(pattern)].map((match) => ({ file, key: match[1] ?? "" })),
  ),
);

describe("translation keys used by the customer UI", () => {
  it("finds the translation calls at all", () => {
    expect(used.length).toBeGreaterThan(5);
  });

  it("only uses keys the resources define", () => {
    // Plural keys are addressed by their base name; the resources hold the _one/_other variants.
    const known = new Set(
      flatten(customerEn).map((key) => key.replace(/_(one|other|zero|two|few|many)$/, "")),
    );
    expect(used.filter((entry) => !known.has(entry.key))).toEqual([]);
  });

  /**
   * The words are loaded in slices (issue #99): `core.ts` on every `/app` visit, and a screen's own
   * slice when its route is entered (`src/i18n/slices.ts`, loaded in `router.tsx`). A key that
   * exists but is in a slice this screen's route does not load renders the key itself to a paying
   * customer — invisible to the test above, which reads every slice merged, and invisible to
   * TypeScript, which cannot see inside a string. So each SPA file is checked against exactly the
   * words its route has.
   *
   * The Next app is not checked here: it renders on the server with the whole resource in hand
   * (`customer/lib/i18n.ts`), which is why no resource file is ever a client chunk there.
   */
  it("only names words the route it is on actually loads", () => {
    const strip = (key: string) => key.replace(/_(one|other|zero|two|few|many)$/, "");
    const wordsOf = (file: string) => {
      const allowed = new Set(flatten(enCore).map(strip));
      for (const slice of FILE_SLICES[file] ?? []) {
        for (const key of flatten(SLICE_WORDS[slice])) allowed.add(strip(key));
      }
      return allowed;
    };
    const offenders: string[] = [];
    for (const { file, key } of used) {
      if (!file.startsWith("./")) continue;
      if (!wordsOf(file).has(strip(key))) offenders.push(`${file} names ${key}`);
    }
    // A template-literal key (`t(\`rights.works.blockers.${code}\`)`) is only knowable up to its
    // static head; the route must at least have words under it.
    for (const [file, source] of Object.entries(sources)) {
      if (!file.startsWith("./")) continue;
      const allowed = [...wordsOf(file)];
      for (const match of source.matchAll(PREFIX_PATTERN)) {
        const prefix = match[1] ?? "";
        if (!allowed.some((key) => key.startsWith(prefix))) offenders.push(`${file} names ${prefix}…`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
