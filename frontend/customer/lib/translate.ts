/**
 * The customer app's translator (ADR 0036), in place of i18next.
 *
 * The resources in `frontend/src/i18n/{en,de}.ts` use exactly two i18next features, and this
 * implements both:
 *
 * - **`{{placeholder}}` interpolation.** Values are substituted verbatim; React escapes what it
 *   renders, so nothing is escaped here (the same reason `interpolation.escapeValue` was false).
 * - **`_one` / `_other` plural suffixes**, resolved with `Intl.PluralRules` for the active
 *   language — the rule set i18next uses too. German and English both have one/other, but the
 *   category comes from `Intl`, not from `count === 1`, so a language with more categories would
 *   only need its suffixes in the resources.
 *
 * Why not i18next: it costs ~14 KB gzip in the browser, and its remaining value was switching
 * language at runtime in a client-rendered app. Here the language is a property of the request,
 * the server renders the words into HTML, and an interactive island gets the slice of the
 * resources it needs as props — so no resource file is ever a client module.
 *
 * A missing key returns the key itself, exactly as i18next does with no fallback resource: a typo
 * shows up as `nav.overview` on screen, and `translationKeys.test.ts` fails the build for it.
 */

/** The resource sets are deeply nested objects of strings. */
export type Resource = { readonly [key: string]: string | Resource };
/**
 * `undefined` is allowed for one reason: the shared logic hands over messages whose `count` is set
 * only for the keys that have plural forms (`FailureMessage`, `SignInMessage`), and a caller should
 * not have to build a different object per branch. An undefined value leaves its placeholder
 * visible rather than printing "undefined".
 */
export type Values = Readonly<Record<string, string | number | undefined>>;

/** A translator bound to one language and one resource set. */
export type Translate = (key: string, values?: Values) => string;

function lookup(resource: Resource, key: string): string | undefined {
  let node: string | Resource | undefined = resource;
  for (const part of key.split(".")) {
    if (typeof node !== "object") return undefined;
    node = node[part];
  }
  return typeof node === "string" ? node : undefined;
}

function interpolate(template: string, values: Values | undefined): string {
  if (!values) return template;
  return template.replace(/\{\{(\w+)\}\}/g, (whole, name: string) => {
    const value = values[name];
    return value === undefined ? whole : String(value);
  });
}

/**
 * Build a translator for one language over one resource set.
 *
 * With a `count` value the plural suffix is tried first (`key_one`, `key_other`, …) and the
 * unsuffixed key is the fallback, so a key that has no plural forms still works with a count.
 */
export function translator(resource: Resource, language: string): Translate {
  const plurals = new Intl.PluralRules(language);
  return (key, values) => {
    let template: string | undefined;
    const count = values?.count;
    if (typeof count === "number") template = lookup(resource, `${key}_${plurals.select(count)}`);
    template ??= lookup(resource, key);
    if (template === undefined) return key;
    return interpolate(template, values);
  };
}

/**
 * The part of a resource set an interactive island needs, by its top-level key
 * (`slice(resource, "signIn")`). The island renders words the server already chose the language
 * for; nothing bigger than the island's own section travels to the browser.
 */
export function slice(resource: Resource, ...keys: readonly string[]): Resource {
  const out: Record<string, string | Resource> = {};
  for (const key of keys) {
    const node = resource[key];
    if (node !== undefined) out[key] = node;
  }
  return out;
}
