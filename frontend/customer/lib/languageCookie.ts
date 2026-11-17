/**
 * Remembering the language choice, from the browser's side (ADR 0036).
 *
 * The server reads `marceline.language` on every request (`requestLanguage.ts`), so the switch
 * writes a cookie rather than `localStorage`: a preference the *server* has to see, which is what
 * makes one language per load possible at all. Not `HttpOnly` — it is a preference, not a
 * credential, and this script sets it without a round trip.
 *
 * Its own module so the writing is not an assignment inside a component body.
 */
import { LANGUAGE_COOKIE_MAX_AGE, LANGUAGE_KEY, type Language } from "./requestLanguage";

/** The cookie string for a language choice. Pure, so the attributes are a test. */
export function languageCookie(language: Language): string {
  return `${LANGUAGE_KEY}=${language}; path=/app; max-age=${LANGUAGE_COOKIE_MAX_AGE}; samesite=lax`;
}

export function rememberLanguage(language: Language): void {
  if (typeof document === "undefined") return;
  document.cookie = languageCookie(language);
}
