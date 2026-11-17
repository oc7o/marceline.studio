/**
 * Which screen's words live in which slice (issue #99).
 *
 * The customer words are loaded in pieces: `en/core.ts` on every `/app` visit, and one slice per
 * heavy screen when that screen's route is entered. This module is the single place that says
 * which screen needs which — `router.tsx` loads by these names, and
 * `src/app/translationKeys.test.ts` checks against them that a screen never names a key from a
 * slice its route does not load, which would render the key itself to a paying customer.
 *
 * It deliberately imports nothing: the test reads it without pulling i18next in.
 */

/** A slice of the customer words beyond the core one. */
export type Slice = "caseFile" | "addOriginal" | "rights" | "notices";

/** Every screen whose route needs more than the core words, by module name. */
export const SCREEN_WORDS = {
  CaseFileScreen: ["caseFile"],
  AddOriginalScreen: ["addOriginal"],
  RightsScreen: ["rights"],
  NoticesScreen: ["notices"],
  NoticeDraftScreen: ["notices"],
} as const satisfies Record<string, readonly Slice[]>;

/** A screen that names words beyond the core slice. */
export type WordedScreen = keyof typeof SCREEN_WORDS;

/** Every slice there is, for the tests and the language switch. */
export const SLICES: readonly Slice[] = ["caseFile", "addOriginal", "rights", "notices"];
