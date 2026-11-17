/**
 * Which language the customer UI speaks, and how that choice is remembered.
 *
 * Pure functions with an injected storage and language list, so the rules are testable without a
 * browser (the house pattern, see `lib/imageMode.ts`). Order of preference:
 *
 * 1. an explicit choice this browser made before (`localStorage`);
 * 2. the browser's own languages, first recognised one wins ("de-AT" counts as German);
 * 3. English — the safer guess for a browser asking for neither, since the alternative would be
 *    showing German to an international creator who cannot read it.
 */
export const LANGUAGES = ["de", "en"] as const;
export type Language = (typeof LANGUAGES)[number];

export const LANGUAGE_KEY = "marceline.language";
export const FALLBACK_LANGUAGE: Language = "en";

type ReadableStorage = Pick<Storage, "getItem">;
type WritableStorage = Pick<Storage, "setItem">;

export function isLanguage(value: unknown): value is Language {
  return typeof value === "string" && (LANGUAGES as readonly string[]).includes(value);
}

/** First language tag we recognise, ignoring the region: "de-AT" → "de". */
export function matchLanguage(tags: readonly string[]): Language | null {
  for (const tag of tags) {
    const base = tag.trim().toLowerCase().split("-")[0];
    if (isLanguage(base)) return base;
  }
  return null;
}

export function readStoredLanguage(storage: ReadableStorage | null | undefined): Language | null {
  try {
    const stored = storage?.getItem(LANGUAGE_KEY);
    return isLanguage(stored) ? stored : null;
  } catch {
    // Storage blocked or absent: fall back to the browser's languages.
    return null;
  }
}

export function writeStoredLanguage(
  storage: WritableStorage | null | undefined,
  language: Language,
): void {
  try {
    storage?.setItem(LANGUAGE_KEY, language);
  } catch {
    // Storage blocked: the choice lasts for this page only.
  }
}

/** The language to start in: stored choice, else the browser's, else English. */
export function initialLanguage(
  storage: ReadableStorage | null | undefined,
  browserLanguages: readonly string[],
): Language {
  return readStoredLanguage(storage) ?? matchLanguage(browserLanguages) ?? FALLBACK_LANGUAGE;
}

function browserStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/** What this browser asks for, most preferred first. */
export function browserLanguages(): readonly string[] {
  if (typeof navigator === "undefined") return [];
  return navigator.languages ?? (navigator.language ? [navigator.language] : []);
}

export function detectLanguage(): Language {
  return initialLanguage(browserStorage(), browserLanguages());
}

/** Remember an explicit choice for the next visit. */
export function persistLanguage(language: Language): void {
  writeStoredLanguage(browserStorage(), language);
}
