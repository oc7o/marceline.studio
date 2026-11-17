import { describe, expect, it } from "vitest";

import { resolveTargetId } from "./creatorReview";

describe("resolveTargetId", () => {
  it("prefers a picked suggestion over a typed id", () => {
    expect(resolveTargetId(7, "999")).toBe(7);
  });

  it("falls back to a valid typed id when nothing is picked", () => {
    expect(resolveTargetId(null, "42")).toBe(42);
    expect(resolveTargetId(null, "  42  ")).toBe(42);
  });

  it("is null when neither is set", () => {
    expect(resolveTargetId(null, "")).toBeNull();
    expect(resolveTargetId(null, "   ")).toBeNull();
  });

  it("rejects a typed id that is not a positive integer", () => {
    expect(resolveTargetId(null, "0")).toBeNull();
    expect(resolveTargetId(null, "-3")).toBeNull();
    expect(resolveTargetId(null, "3.5")).toBeNull();
    expect(resolveTargetId(null, "abc")).toBeNull();
  });
});
