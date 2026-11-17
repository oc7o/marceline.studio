import { describe, expect, it } from "vitest";

import { de } from "@shared/i18n/de";
import { en } from "@shared/i18n/en";

import { type Resource, slice, translator } from "./translate";

const words: Resource = {
  shell: { brand: "Marceline", signedInAs: "Signed in as {{username}}" },
  copies_one: "{{count}} matching copy, {{removed}} removed",
  copies_other: "{{count}} matching copies, {{removed}} removed",
  plain: "no placeholders",
};

describe("translator", () => {
  const t = translator(words, "en");

  it("walks a dotted path", () => {
    expect(t("shell.brand")).toBe("Marceline");
  });

  it("interpolates every placeholder", () => {
    expect(t("shell.signedInAs", { username: "ada@example.com" })).toBe(
      "Signed in as ada@example.com",
    );
  });

  it("leaves a placeholder alone when no value was passed", () => {
    // Better a visible hole than a silent empty string: the key is in the resources, so
    // `translationKeys.test.ts` cannot catch this and the screen must.
    expect(t("shell.signedInAs")).toBe("Signed in as {{username}}");
  });

  it("picks the plural form from the count", () => {
    expect(t("copies", { count: 1, removed: 0 })).toBe("1 matching copy, 0 removed");
    expect(t("copies", { count: 4, removed: 2 })).toBe("4 matching copies, 2 removed");
    expect(t("copies", { count: 0, removed: 0 })).toBe("0 matching copies, 0 removed");
  });

  it("falls back to the unsuffixed key when a count meets a singular string", () => {
    expect(t("plain", { count: 3 })).toBe("no placeholders");
  });

  it("returns the key itself when nothing is defined", () => {
    // Exactly what i18next does without a fallback resource, and what the key test looks for.
    expect(t("shell.missing")).toBe("shell.missing");
    expect(t("nope")).toBe("nope");
  });

  it("does not mistake a section for a string", () => {
    expect(t("shell")).toBe("shell");
  });
});

describe("the real resources", () => {
  it("renders English plurals and interpolations", () => {
    const t = translator(en as unknown as Resource, "en");
    expect(t("overview.confirmed", { count: 1, removed: 0 })).toBe(
      "1 human-reviewed matching copy, 0 of them removed",
    );
    expect(t("overview.confirmed", { count: 3, removed: 1 })).toBe(
      "3 human-reviewed matching copies, 1 of them removed",
    );
    expect(t("overview.summary.found_online", { count: 2 })).toBe("2 with copies online");
  });

  it("renders German plurals and interpolations", () => {
    const t = translator(de as unknown as Resource, "de");
    expect(t("overview.confirmed", { count: 1, removed: 0 })).toBe(
      "1 manuell geprüfte übereinstimmende Kopie, davon 0 entfernt",
    );
    expect(t("overview.confirmed", { count: 3, removed: 1 })).toBe(
      "3 manuell geprüfte übereinstimmende Kopien, davon 1 entfernt",
    );
  });

  it("leaves no placeholder unfilled in any string whose values the tests know", () => {
    const t = translator(en as unknown as Resource, "en");
    expect(t("shell.signedInAs", { username: "x" })).not.toContain("{{");
  });
});

describe("slice", () => {
  it("keeps only the named sections", () => {
    const only = slice(words, "shell", "absent");
    expect(Object.keys(only)).toEqual(["shell"]);
    expect(translator(only, "en")("shell.brand")).toBe("Marceline");
  });
});
