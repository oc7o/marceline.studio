import { describe, expect, it } from "vitest";

import { languageFor, parseAcceptLanguage } from "./requestLanguage";

describe("parseAcceptLanguage", () => {
  it("takes the first recognised tag, region ignored", () => {
    expect(parseAcceptLanguage("de-AT,de;q=0.9,en;q=0.8")).toBe("de");
    expect(parseAcceptLanguage("en-GB,en;q=0.9")).toBe("en");
  });

  it("honours quality values over header order", () => {
    expect(parseAcceptLanguage("fr;q=1.0,de;q=0.9,en;q=0.95")).toBe("en");
  });

  it("ignores languages we do not speak", () => {
    expect(parseAcceptLanguage("fr-FR,fr;q=0.9,it;q=0.8")).toBeNull();
    expect(parseAcceptLanguage("*")).toBeNull();
  });

  it("ignores a tag the browser explicitly does not want", () => {
    expect(parseAcceptLanguage("de;q=0,en;q=0.5")).toBe("en");
  });

  it("survives an absent or empty header", () => {
    expect(parseAcceptLanguage(null)).toBeNull();
    expect(parseAcceptLanguage("")).toBeNull();
    expect(parseAcceptLanguage(" , ;q=0.5")).toBeNull();
  });
});

describe("languageFor", () => {
  it("prefers the remembered choice", () => {
    expect(languageFor("de", "en-GB,en;q=0.9")).toBe("de");
    expect(languageFor("en", "de-DE")).toBe("en");
  });

  it("ignores a cookie value that is not a language we speak", () => {
    expect(languageFor("klingon", "de-DE")).toBe("de");
    expect(languageFor("", "de-DE")).toBe("de");
  });

  it("falls back to English for a browser asking for neither", () => {
    expect(languageFor(null, "fr-FR")).toBe("en");
    expect(languageFor(null, null)).toBe("en");
  });
});
