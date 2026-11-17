/**
 * Both languages together, for tests only — never import this from runtime code.
 *
 * `i18n/index.ts` deliberately loads one language at a time (ADR 0028 performance budget, issue
 * #82) and, since issue #99, only the slice of words the screen in view names. This module exists
 * so the tests can still check the whole resource — every key, every interpolation placeholder,
 * the wording rules, and that every key a screen names exists — without duplicating the
 * composition. A static import of `en.ts` and `de.ts` here is fine because nothing shipped to the
 * browser imports this file; only test files do.
 */
import { de } from "./de";
import { en } from "./en";

/** Both languages, every slice put back together. */
export const resources = {
  en: { translation: en },
  de: { translation: de },
} as const;

/** The full English resource a customer screen may name keys from. */
export const customerEn = resources.en.translation;
/** The full German resource. */
export const customerDe = resources.de.translation;
