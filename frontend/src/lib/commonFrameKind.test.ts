import { describe, expect, it } from "vitest";

import { COMMON_FRAME_KINDS, COMMON_FRAME_STATUSES, kindLabel, wouldDiscount } from "./commonFrameKind";

describe("kindLabel", () => {
  it("has words for every stored kind", () => {
    for (const kind of COMMON_FRAME_KINDS) {
      expect(kindLabel(kind)).not.toBe(kind);
    }
  });

  it("falls back to the raw value for an unknown kind", () => {
    expect(kindLabel("something_new")).toBe("something_new");
  });
});

describe("wouldDiscount", () => {
  it("is true only for confirmed, not-not_common groups", () => {
    for (const kind of COMMON_FRAME_KINDS) {
      for (const status of COMMON_FRAME_STATUSES) {
        expect(wouldDiscount(kind, status)).toBe(status === "confirmed" && kind !== "not_common");
      }
    }
  });
});
