/**
 * The i18n seam for the customer UI (ADR 0026).
 *
 * i18next + react-i18next, resources bundled as TypeScript modules so they are typechecked and
 * need no fetch at startup — but only the **active** language ever loads (ADR 0028 performance
 * budget, issue #82): `en.ts` and `de.ts` are about 600 lines each, and bundling both unconditionally
 * cost every `/app` visitor the language they cannot read. `ensureI18nReady()` dynamically imports
 * the detected language and initialises i18next with only that one; `useLanguage`'s setter loads
 * the other language, once, the first time someone actually switches. The staff SPA is untouched:
 * only the customer subtree ever imports this module.
 *
 * **And only the words of the screen in view** (issue #99): the first load carries `en/core.ts`
 * alone — the shell, the sign-in form, the overview and the small screens — and a route whose
 * screen is listed in `slices.ts` loads that screen's slice in its loader before it renders. The
 * four sliced screens are four fifths of the words, so this took the SPA's `/app` first load from
 * 149.88 KB gzip (0.12 KB under the guard) to well under it, and a sentence added to one screen no
 * longer costs every visitor bytes. `resources.ts` still puts every slice back together for the
 * tests, so key parity, placeholder parity, the wording rules and "every key a screen names
 * exists" cover every string exactly as before.
 */
import i18next, { type i18n as I18n } from "i18next";
import { useCallback } from "react";
import { initReactI18next, useTranslation } from "react-i18next";

import type { Translation } from "./en";
import {
  FALLBACK_LANGUAGE,
  type Language,
  detectLanguage,
  isLanguage,
  persistLanguage,
} from "./language";
import { SCREEN_WORDS, SLICES, type Slice, type WordedScreen } from "./slices";

export const i18n: I18n = i18next.createInstance();

/**
 * A piece of a language: the core words, or one screen's slice. Deeply partial, because three
 * sections are split across two modules (`en.ts`) — `Partial<Translation>` would insist that
 * whichever module holds `addOriginal` holds all of it.
 */
type Words = { [K in keyof Translation]?: DeepPartial<Translation[K]> };
type DeepPartial<T> = T extends string ? T : { [K in keyof T]?: DeepPartial<T[K]> };

/**
 * Every dynamic import, written out. A template-literal `import()` would work in Vite too, but an
 * explicit map is what makes each chunk visible to the bundle-size check — and to a reader asking
 * which words a route pays for.
 */
const LOADERS: Record<Language, Record<"core" | Slice, () => Promise<Words>>> = {
  en: {
    core: () => import("./en/core").then((module) => module.enCore),
    caseFile: () => import("./en/caseFile").then((module) => module.enCaseFile),
    addOriginal: () => import("./en/addOriginal").then((module) => module.enAddOriginal),
    rights: () => import("./en/rights").then((module) => module.enRights),
    notices: () => import("./en/notices").then((module) => module.enNotices),
  },
  de: {
    core: () => import("./de/core").then((module) => module.deCore),
    caseFile: () => import("./de/caseFile").then((module) => module.deCaseFile),
    addOriginal: () => import("./de/addOriginal").then((module) => module.deAddOriginal),
    rights: () => import("./de/rights").then((module) => module.deRights),
    notices: () => import("./de/notices").then((module) => module.deNotices),
  },
};

/** The slices some screen has already asked for, so a language switch can reload exactly those. */
const wanted = new Set<Slice>();
/** `${language}:${slice}` pairs already merged into the instance. */
const merged = new Set<string>();

async function mergeWords(instance: I18n, language: Language, slice: "core" | Slice): Promise<void> {
  const mark = `${language}:${slice}`;
  if (merged.has(mark)) return;
  const words = await LOADERS[language][slice]();
  // Deep merge, overwriting: three sections are split across the core slice and a screen's slice
  // (see `en.ts`), so a shallow merge would drop whichever half arrived first.
  instance.addResourceBundle(language, "translation", words, true, true);
  merged.add(mark);
}

let ready: Promise<void> | null = null;

/**
 * Loads and initialises the detected language's **core** words, once. Call this from the `/app`
 * route's loader (`router.tsx`), never from a component body: `CustomerApp` must not render before
 * the instance has resources, because `react: { useSuspense: false }` means a missing bundle
 * renders raw keys instead of suspending.
 */
export function ensureI18nReady(): Promise<void> {
  if (!ready) {
    const language = detectLanguage();
    ready = LOADERS[language]
      .core()
      .then((resource) => {
        merged.add(`${language}:core`);
        return i18n.use(initReactI18next).init({
          resources: { [language]: { translation: resource } },
          lng: language,
          fallbackLng: FALLBACK_LANGUAGE,
          // React escapes for us; escaping here would double-encode.
          interpolation: { escapeValue: false },
          react: { useSuspense: false },
        });
      })
      .then(() => undefined);
  }
  return ready;
}

/** The language the instance is on, once it is initialised. */
function currentLanguage(): Language {
  const resolved = i18n.resolvedLanguage;
  return isLanguage(resolved) ? resolved : FALLBACK_LANGUAGE;
}

/**
 * Load the words a screen names, before it renders. A route's loader awaits this; it implies
 * `ensureI18nReady()`, so it is safe whichever order the router runs the loaders in.
 */
export async function loadScreenWords(screen: WordedScreen): Promise<void> {
  await ensureI18nReady();
  const language = currentLanguage();
  for (const slice of SCREEN_WORDS[screen]) {
    wanted.add(slice);
    await mergeWords(i18n, language, slice);
  }
}

/** Load a language's resources — the core words plus every slice already asked for — and switch. */
async function switchLanguage(instance: I18n, language: Language): Promise<void> {
  await mergeWords(instance, language, "core");
  for (const slice of wanted) await mergeWords(instance, language, slice);
  await instance.changeLanguage(language);
}

/** Keep the document's language in step, for screen readers and hyphenation. */
i18n.on("languageChanged", (language: string) => {
  if (typeof document !== "undefined") document.documentElement.lang = language;
});

/** The language in use, and a setter that remembers the choice and loads its resources first. */
export function useLanguage(): [Language, (language: Language) => void] {
  const { i18n: instance } = useTranslation();
  const resolved = instance.resolvedLanguage;
  const current = isLanguage(resolved) ? resolved : FALLBACK_LANGUAGE;
  const setLanguage = useCallback(
    (language: Language) => {
      void switchLanguage(instance, language).then(() => persistLanguage(language));
    },
    [instance],
  );
  return [current, setLanguage];
}

/**
 * Test-only: ensure the instance is initialised and switch it to a language with **every** slice
 * loaded, because a test renders whichever screen it likes. Never imported by runtime code, so it
 * does not ship — a route loads only its own slice (`loadScreenWords`).
 */
export async function setLanguageForTest(language: Language): Promise<void> {
  await ensureI18nReady();
  for (const slice of SLICES) wanted.add(slice);
  await switchLanguage(i18n, language);
}
