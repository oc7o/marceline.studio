import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { rateLimitedMessage, signInMessage } from "./signInError";

describe("signInMessage", () => {
  it("says one thing for every credential failure (401)", () => {
    const unknownAccount = new ApiError(401, "Invalid username or password", {
      detail: "Invalid username or password",
    });
    const wrongPassword = new ApiError(401, "Invalid username or password", {
      detail: "Invalid username or password",
    });
    expect(signInMessage(unknownAccount)).toEqual({ key: "signIn.errors.invalid" });
    // The same message for both: whether the account exists is exactly what must not leak.
    expect(signInMessage(wrongPassword)).toEqual(signInMessage(unknownAccount));
  });

  it("explains a 403 for an account with no organisation", () => {
    const error = new ApiError(403, "This account has no customer organisation.", {});
    expect(signInMessage(error)).toEqual({ key: "signIn.errors.noOrganisation" });
  });

  it("does not blame the account for a 403 that was really a CSRF failure", () => {
    const error = new ApiError(403, "CSRF check failed", { detail: "CSRF check failed" });
    expect(signInMessage(error)).toEqual({ key: "signIn.errors.formExpired" });
  });

  it("states the wait for a 429", () => {
    expect(signInMessage(new ApiError(429, "Too many", {}, 900))).toEqual({
      key: "signIn.errors.rateLimitedMinutes",
      values: { count: 15 },
    });
  });

  it("falls back to a plain message for an unexpected status", () => {
    expect(signInMessage(new ApiError(500, "boom", {}))).toEqual({
      key: "signIn.errors.unexpected",
      values: { status: 500 },
    });
  });

  it("reports an unreachable server for anything that is not an API error", () => {
    expect(signInMessage(new TypeError("Failed to fetch"))).toEqual({ key: "common.unreachable" });
    expect(signInMessage(undefined)).toEqual({ key: "common.unreachable" });
  });
});

describe("rateLimitedMessage", () => {
  it("counts in seconds below a minute", () => {
    expect(rateLimitedMessage(30)).toEqual({
      key: "signIn.errors.rateLimitedSeconds",
      values: { count: 30 },
    });
  });

  it("rounds up to whole minutes", () => {
    expect(rateLimitedMessage(61)).toEqual({
      key: "signIn.errors.rateLimitedMinutes",
      values: { count: 2 },
    });
    expect(rateLimitedMessage(60)).toEqual({
      key: "signIn.errors.rateLimitedMinutes",
      values: { count: 1 },
    });
  });

  it("says only 'later' when the server sent no Retry-After", () => {
    expect(rateLimitedMessage(null)).toEqual({ key: "signIn.errors.rateLimited" });
    expect(rateLimitedMessage(0)).toEqual({ key: "signIn.errors.rateLimited" });
  });
});
