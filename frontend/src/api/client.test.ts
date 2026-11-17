import { describe, expect, it } from "vitest";

import { ApiError, parseRetryAfter, readCookie } from "./client";

const NOW = Date.parse("2026-09-16T12:00:00Z");

describe("parseRetryAfter", () => {
  it("reads a number of seconds", () => {
    expect(parseRetryAfter("900")).toBe(900);
    expect(parseRetryAfter(" 42 ")).toBe(42);
    expect(parseRetryAfter("0")).toBe(0);
  });

  it("reads the HTTP-date form as seconds from now", () => {
    expect(parseRetryAfter("Wed, 16 Sep 2026 12:02:00 GMT", NOW)).toBe(120);
  });

  it("never returns a negative wait for a date in the past", () => {
    expect(parseRetryAfter("Wed, 16 Sep 2026 11:00:00 GMT", NOW)).toBe(0);
  });

  it("returns null when the header is absent or unusable", () => {
    expect(parseRetryAfter(null)).toBeNull();
    expect(parseRetryAfter(undefined)).toBeNull();
    expect(parseRetryAfter("")).toBeNull();
    expect(parseRetryAfter("soon")).toBeNull();
  });
});

describe("ApiError", () => {
  it("has no wait unless one was given", () => {
    expect(new ApiError(401, "nope", null).retryAfterSeconds).toBeNull();
    expect(new ApiError(429, "later", null, 900).retryAfterSeconds).toBe(900);
  });
});

describe("readCookie", () => {
  it("finds the CSRF token among other cookies", () => {
    expect(readCookie("csrftoken", "sessionid=abc; csrftoken=tok123")).toBe("tok123");
    expect(readCookie("csrftoken", "sessionid=abc")).toBeNull();
  });
});
