/**
 * The server side of i18n (ADR 0036): the request's language, its resources and a translator.
 *
 * `server-only` so a client component that imports this fails the **build** rather than quietly
 * shipping ~9 KB gzip of resource file to every browser. Islands take the words they need as
 * props (`slice()` in `translate.ts`).
 *
 * The resources are loaded dynamically per language and memoised, so a process that only ever
 * answers German requests never holds the English file, and neither file is in the entry module.
 */
import "server-only";
import { cookies, headers } from "next/headers";

import { LANGUAGE_KEY, type Language, languageFor } from "./requestLanguage";
import { type Resource, type Translate, translator } from "./translate";

const loaded = new Map<Language, Resource>();

async function resourceFor(language: Language): Promise<Resource> {
  const cached = loaded.get(language);
  if (cached) return cached;
  // `en.ts` / `de.ts` compose every slice (issue #99): this app renders on the server, so it takes
  // the whole resource and ships none of it. Only the SPA loads the slices one at a time.
  const resource = (
    language === "de" ? (await import("@shared/i18n/de")).de : (await import("@shared/i18n/en")).en
  ) as unknown as Resource;
  loaded.set(language, resource);
  return resource;
}

export interface RequestI18n {
  language: Language;
  resource: Resource;
  t: Translate;
}

/**
 * The language, words and translator for the request being answered. Safe to call from several
 * components of one page: the resources are memoised and the translator is a closure over
 * immutable data, so there is no per-request instance to leak between two customers.
 */
export async function requestI18n(): Promise<RequestI18n> {
  const [cookieStore, headerList] = await Promise.all([cookies(), headers()]);
  const language = languageFor(
    cookieStore.get(LANGUAGE_KEY)?.value,
    headerList.get("accept-language"),
  );
  const resource = await resourceFor(language);
  return { language, resource, t: translator(resource, language) };
}
