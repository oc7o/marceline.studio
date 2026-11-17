import { describe, expect, it } from "vitest";

import {
  FALLBACK_LANGUAGE,
  LANGUAGE_KEY,
  initialLanguage,
  isLanguage,
  matchLanguage,
  readStoredLanguage,
  writeStoredLanguage,
} from "./language";

function storage(value: string | null): Pick<Storage, "getItem"> {
  return { getItem: (key: string) => (key === LANGUAGE_KEY ? value : null) };
}

const BLOCKED: Pick<Storage, "getItem" | "setItem"> = {
  getItem: () => {
    throw new Error("storage blocked");
  },
  setItem: () => {
    throw new Error("storage blocked");
  },
};

describe("isLanguage", () => {
  it("accepts the languages we ship and nothing else", () => {
    expect(isLanguage("de")).toBe(true);
    expect(isLanguage("en")).toBe(true);
    expect(isLanguage("fr")).toBe(false);
    expect(isLanguage("de-AT")).toBe(false);
    expect(isLanguage(undefined)).toBe(false);
  });
});

describe("matchLanguage", () => {
  it("ignores the region", () => {
    expect(matchLanguage(["de-AT"])).toBe("de");
    expect(matchLanguage(["EN-gb"])).toBe("en");
  });

  it("takes the first language it recognises", () => {
    expect(matchLanguage(["fr-FR", "de-DE", "en"])).toBe("de");
  });

  it("returns null when it recognises none", () => {
    expect(matchLanguage([])).toBeNull();
    expect(matchLanguage(["fr", "es"])).toBeNull();
  });
});

describe("readStoredLanguage", () => {
  it("reads a stored choice and ignores anything else", () => {
    expect(readStoredLanguage(storage("de"))).toBe("de");
    expect(readStoredLanguage(storage("klingon"))).toBeNull();
    expect(readStoredLanguage(storage(null))).toBeNull();
    expect(readStoredLanguage(null)).toBeNull();
  });

  it("survives storage that throws", () => {
    expect(readStoredLanguage(BLOCKED)).toBeNull();
  });
});

describe("writeStoredLanguage", () => {
  it("stores the choice under one key", () => {
    const written: Record<string, string> = {};
    writeStoredLanguage({ setItem: (key, value) => void (written[key] = value) }, "de");
    expect(written).toEqual({ [LANGUAGE_KEY]: "de" });
  });

  it("survives storage that throws", () => {
    expect(() => writeStoredLanguage(BLOCKED, "de")).not.toThrow();
  });
});

describe("initialLanguage", () => {
  it("prefers an explicit choice over the browser", () => {
    expect(initialLanguage(storage("en"), ["de-DE"])).toBe("en");
    expect(initialLanguage(storage("de"), ["en-US"])).toBe("de");
  });

  it("falls back to the browser when nothing was chosen", () => {
    expect(initialLanguage(storage(null), ["de-DE", "en"])).toBe("de");
  });

  it("falls back to English for a browser asking for neither language", () => {
    expect(initialLanguage(storage(null), ["fr-FR"])).toBe(FALLBACK_LANGUAGE);
    expect(initialLanguage(null, [])).toBe(FALLBACK_LANGUAGE);
  });
});
