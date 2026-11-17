/**
 * Which language a request is answered in (ADR 0036).
 *
 * One language per load, as in phase 1 — but chosen on the server, so `<html lang>` is right in
 * the first byte and no resource file is ever a client module. Order of preference:
 *
 * 1. the `marceline.language` cookie — the same name the SPA used, so a customer who switched
 *    there and comes back here is not surprised;
 * 2. the browser's `Accept-Language`, first recognised tag, region ignored ("de-AT" counts as
 *    German);
 * 3. English, because showing German to an international creator who cannot read it is the worse
 *    failure (ADR 0026).
 *
 * A choice the SPA only ever put in `localStorage` is invisible to a server, so such a visitor is
 * detected from `Accept-Language` once and re-pinned by their first switch here.
 *
 * Pure functions over the two header values, so every branch is a test rather than a browser.
 */
import { FALLBACK_LANGUAGE, LANGUAGE_KEY, type Language, isLanguage } from "@shared/i18n/language";

export { FALLBACK_LANGUAGE, LANGUAGE_KEY, type Language, isLanguage };

/**
 * How long the switch remembers a choice, in seconds. A preference, so it is not `HttpOnly`.
 *
 * **180 days, not a year**. The
 * cookie is written only by the customer's own click on the language switch, and it is kept no
 * longer than the preference needs. Half a year still spans the gaps between a customer's visits. This constant
 * is the single place the lifetime is stated; the service privacy notice cites it.
 */
export const LANGUAGE_COOKIE_DAYS = 180;
export const LANGUAGE_COOKIE_MAX_AGE = LANGUAGE_COOKIE_DAYS * 24 * 60 * 60;

/**
 * `Accept-Language` in preference order. Quality values are honoured, ties keep header order —
 * which is what every browser sends anyway.
 */
export function parseAcceptLanguage(header: string | null | undefined): Language | null {
  if (!header) return null;
  const tags = header
    .split(",")
    .map((part, index) => {
      const [tag, ...params] = part.trim().split(";");
      const q = params.map((p) => /^\s*q=([\d.]+)\s*$/.exec(p)).find(Boolean);
      const quality = q?.[1] === undefined ? 1 : Number(q[1]);
      return { tag: (tag ?? "").trim().toLowerCase(), quality, index };
    })
    .filter((entry) => entry.tag !== "" && entry.quality > 0)
    .sort((a, b) => b.quality - a.quality || a.index - b.index);
  for (const { tag } of tags) {
    const base = tag.split("-")[0];
    if (isLanguage(base)) return base;
  }
  return null;
}

/** The language for one request: the cookie, then the header, then English. */
export function languageFor(
  cookieValue: string | null | undefined,
  acceptLanguage: string | null | undefined,
): Language {
  if (isLanguage(cookieValue)) return cookieValue;
  return parseAcceptLanguage(acceptLanguage) ?? FALLBACK_LANGUAGE;
}
