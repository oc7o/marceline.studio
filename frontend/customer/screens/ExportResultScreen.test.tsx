/**
 * `/app/export` (issue #131): the message a customer sees when "Download my data" did not result
 * in a file — never the refusal's JSON body saved as a download.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { de } from "@shared/i18n/de";
import { en } from "@shared/i18n/en";

import { type Resource, translator } from "../lib/translate";
import type { Words } from "../lib/words";
import { ExportResultScreen, exportProblemFromParam } from "./ExportResultScreen";

const WORDS: Record<"en" | "de", Words> = {
  en: { t: translator(en as unknown as Resource, "en"), language: "en" },
  de: { t: translator(de as unknown as Resource, "de"), language: "de" },
};

describe("exportProblemFromParam", () => {
  it("reads the redirect's own query value", () => {
    expect(exportProblemFromParam("rate_limited")).toBe("rate_limited");
    expect(exportProblemFromParam("not_available")).toBe("not_available");
  });

  it("falls back to the generic problem for anything missing or unrecognised", () => {
    expect(exportProblemFromParam(undefined)).toBe("error");
    expect(exportProblemFromParam("")).toBe("error");
    expect(exportProblemFromParam(["rate_limited", "error"])).toBe("rate_limited");
  });
});

describe("the export result screen", () => {
  it.each(["en", "de"] as const)("states the rate limit as a sentence, in %s", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(<ExportResultScreen i18n={i18n} problem="rate_limited" />);
    expect(html).toContain(i18n.t("shell.export.rateLimited"));
    expect(html).toContain(i18n.t("nav.overview"));
    expect(html).toContain('href="/app"');
  });

  it.each(["en", "de"] as const)("says so when the account has no export, in %s", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(<ExportResultScreen i18n={i18n} problem="not_available" />);
    expect(html).toContain(i18n.t("shell.export.notAvailable"));
  });

  it.each(["en", "de"] as const)("has a plain fallback for anything else, in %s", (language) => {
    const i18n = WORDS[language];
    const html = renderToStaticMarkup(<ExportResultScreen i18n={i18n} problem="error" />);
    expect(html).toContain(i18n.t("shell.export.error"));
  });

  it("never renders the raw JSON a refusal would have carried as a file", () => {
    const html = renderToStaticMarkup(<ExportResultScreen i18n={WORDS.en} problem="rate_limited" />);
    expect(html).not.toContain("detail");
    expect(html).not.toContain("rate_limited\"");
  });
});
