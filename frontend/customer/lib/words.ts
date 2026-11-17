/**
 * What a screen needs to write a sentence: the active language and its translator (ADR 0036).
 *
 * Passed as a prop rather than read from a hook or a context, for two reasons. A server component
 * has no context, and threading it explicitly is what keeps every presentational component
 * **synchronous** — so a test renders one to a string without a request, and nothing in the tree
 * quietly becomes a client component to reach a provider.
 *
 * This module holds no resources and imports nothing, so an island may import it.
 */
import type { Language } from "@shared/i18n/language";

import type { Translate } from "./translate";

export interface Words {
  t: Translate;
  language: Language;
}
