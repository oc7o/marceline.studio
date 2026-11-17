/**
 * Wording rules for the customer UI (frontend/src/app/CLAUDE.md "Wording rules"):
 * Marceline states technical facts, the customer makes legal assertions.
 *
 * Every customer-visible string lives in these resources, so scanning them is the whole audit.
 * A hit here is not a typo to silence: rephrase the string as a fact ("matching copy", "record",
 * "human-reviewed match", "suggested notice channel", "past notices"), in both languages.
 */
import { describe, expect, it } from "vitest";

// The merged resources, not one slice: the words are stored per screen (issue #99) and every one
// of them is customer-visible, so the bans are checked over all of them at once.
import { customerDe, customerEn } from "./resources";

function strings(value: unknown, prefix = ""): [string, string][] {
  if (typeof value === "string") return [[prefix, value]];
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) =>
      strings(child, prefix ? `${prefix}.${key}` : key),
    );
  }
  return [];
}

/**
 * Legal conclusions, enforcement promises, forecasts and guaranteed times. Case-insensitive.
 *
 * The last pattern of each language covers what a customer's own papers *mean*:
 * the product must never generate "your mandate does not
 * authorise a notice", because that is an interpretation of the customer's concrete mandate. A
 * string may say what our record holds ("no entry for notices is recorded"); it may not say what
 * the document covers, authorises or permits, in either direction.
 */
const FORBIDDEN: Record<"en" | "de", RegExp[]> = {
  en: [
    /\bleak/i,
    /infring/i,
    /\billegal/i,
    /\bunlawful/i,
    /\bpira(te|cy)/i,
    /\bverified\b/i,
    /\bconfirm(ed|s)?\b/i,
    /\bcases?\b/i,
    /case file/i,
    /\bescalat/i,
    /on (your|the customer's) behalf/i,
    /\bsend(ing|s)? (it |them |the (draft|notice) )?for you\b|\bsending by us\b|\bwe (will )?send (the |a |your )?(notices?|drafts?|requests?)\b/i,
    /\bhandled\b/i,
    /\b(DSA|DMCA) applies\b/i,
    /\bEU host\b/i,
    /\bwe (will )?remove\b/i,
    /\bguarantee/i,
    /\bchance\b/i,
    /\blikel(y|ihood)\b/i,
    /\bexpect/i,
    /\bforecast|\bpredict/i,
    /\bwithin \d+ (hours?|days?)\b/i,
    /\b(mandate|contract|licen[cs]e|agreement|documents?)\b[^.]{0,80}\b(does not|do not|is not|are not|not)\s+(cover|authoris|authoriz|entitle|permit|allow|support)/i,
  ],
  de: [
    /leak/i,
    /rechtswidrig/i,
    /\billegal/i,
    /urheberrechtsverletz|verletzung/i,
    /pirat/i,
    /verifiziert/i,
    /\bbestätigt/i,
    /\bFall\b|\bFälle\b|Fallakte/i,
    /eskal/i,
    /in (Ihrem|deinem) Namen|für (Sie|dich) (ver)?send|Versand durch uns|wir (ver)?senden (die |eine |Ihre |deine )?(Meldung|Entwurf|Entwürfe|Antr)/i,
    /\berledigt\b/i,
    /\b(DSA|DMCA) gilt\b/i,
    /wir entfernen/i,
    /garant/i,
    /\bchance|aussicht auf/i,
    /wahrscheinlich/i,
    /erwarte/i,
    /prognose|vorhersage/i,
    /innerhalb (von )?\d+ (Stunden?|Tagen?)/i,
    /(vollmacht|\bMandat\b|\bVertrag\b|\bLizenz\b|\bUnterlagen\b|\bDokumente?\b)[^.]{0,80}\b(umfasst|umfassen|berechtigt|deckt|decken|erlaubt|gestattet)\b/i,
  ],
};

describe.each([
  ["en", customerEn],
  ["de", customerDe],
] as const)("customer wording in %s", (language, resources) => {
  it("states facts: no legal verdicts, enforcement promises, forecasts or guaranteed times", () => {
    const hits = strings(resources)
      .flatMap(([key, text]) =>
        FORBIDDEN[language].filter((pattern) => pattern.test(text)).map((pattern) => `${key}: ${pattern} in "${text}"`),
      )
      .sort();
    expect(hits).toEqual([]);
  });

  it("catches what it is meant to catch", () => {
    const samples =
      language === "en"
        ? [
            "Confirmed leak",
            "we remove it on your behalf",
            "Sending it for you:",
            "Sending by us:",
            "Your mandate does not authorise a notice.",
            "The agency mandate does not cover notices.",
          ]
        : [
            "Bestätigter Leak — Fallakte",
            "Versand in deinem Namen:",
            "Versand durch uns:",
            "Wir senden die Meldung für dich",
            "Dein Mandat berechtigt nicht zur Meldung.",
            "Die Agenturvollmacht umfasst keine Meldungen.",
          ];
    for (const sample of samples) expect(FORBIDDEN[language].some((pattern) => pattern.test(sample)), sample).toBe(true);
  });
});
