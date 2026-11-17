import { describe, expect, it } from "vitest";

import { clock, creatorLabel, signedSeconds } from "./format";

describe("creatorLabel", () => {
  it("joins every performer of an original", () => {
    expect(creatorLabel(["Sasha Meyer", "Carla Bremer"])).toBe("Sasha Meyer + Carla Bremer");
    expect(creatorLabel(["Sasha Meyer"])).toBe("Sasha Meyer");
  });

  it("keeps the API order: the primary creator stays first", () => {
    expect(creatorLabel(["Zoe Last", "Aaron First"])).toBe("Zoe Last + Aaron First");
  });

  it("falls back when nothing is named", () => {
    expect(creatorLabel([])).toBe("—");
    expect(creatorLabel(undefined)).toBe("—");
    expect(creatorLabel(null)).toBe("—");
    expect(creatorLabel(["  "])).toBe("—");
    expect(creatorLabel([], "unknown")).toBe("unknown");
  });

  it("drops blank entries but keeps the rest", () => {
    expect(creatorLabel(["Sasha Meyer", " "])).toBe("Sasha Meyer");
  });
});

describe("clock and signedSeconds", () => {
  it("formats known and unknown values", () => {
    expect(clock(3700)).toBe("61:40");
    expect(clock(null)).toBe("—");
    expect(signedSeconds(2)).toBe("+2s");
    expect(signedSeconds(-2)).toBe("-2s");
  });
});
