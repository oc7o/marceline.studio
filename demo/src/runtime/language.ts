/**
 * The language of the "request": the app's own rule (`lib/requestLanguage.ts`) applied to the
 * cookie its language switch writes and to the browser's languages in place of `Accept-Language`.
 */
import { readCookie } from "../../../frontend/customer/lib/csrf";
import { LANGUAGE_KEY, type Language, languageFor } from "../../../frontend/customer/lib/requestLanguage";

export function acceptLanguage(): string {
  return (navigator.languages.length > 0 ? navigator.languages : [navigator.language]).join(",");
}

export function currentLanguage(): Language {
  return languageFor(readCookie(LANGUAGE_KEY, document.cookie), acceptLanguage());
}
