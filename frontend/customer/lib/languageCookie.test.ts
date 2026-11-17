import { describe, expect, it } from "vitest";

import { languageCookie, rememberLanguage } from "./languageCookie";
import { LANGUAGE_COOKIE_DAYS, LANGUAGE_COOKIE_MAX_AGE } from "./requestLanguage";

describe("languageCookie", () => {
  it("remembers a choice for 180 days, not a year", () => {
    // The operator's decision of 2026-09-18. The service
    // privacy notice states this number, so it is asserted here rather than left to a comment.
    expect(LANGUAGE_COOKIE_DAYS).toBe(180);
    expect(LANGUAGE_COOKIE_MAX_AGE).toBe(180 * 24 * 60 * 60);
    expect(languageCookie("de")).toContain(`max-age=${180 * 24 * 60 * 60}`);
  });

  it("scopes the cookie to the app and keeps it same-site", () => {
    expect(languageCookie("de")).toBe(
      "marceline.language=de; path=/app; max-age=15552000; samesite=lax",
    );
    expect(languageCookie("en")).toBe(
      "marceline.language=en; path=/app; max-age=15552000; samesite=lax",
    );
  });

  it("writes nothing where there is no document (the server)", () => {
    expect(() => {
      rememberLanguage("de");
    }).not.toThrow();
  });
});
