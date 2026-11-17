import { describe, expect, it } from "vitest";

import {
  bandClasses,
  bandFilterText,
  bandLabel,
  bandTone,
  confidencePercent,
  cycleBands,
  evidenceNotes,
  formatContribution,
  parseBands,
  parseDecidedBy,
  signalBreakdown,
  signalTotal,
  thresholdsText,
  timelineText,
  toggleBand,
} from "./confidence";

describe("band badge", () => {
  it("names its tones for the band's meaning, not for a hue", () => {
    expect(bandTone("high")).toBe("positive");
    expect(bandTone("review")).toBe("caution");
    expect(bandTone("low")).toBe("neutral");
    expect(bandClasses("high")).toContain("positive");
    expect(bandClasses("review")).toContain("caution");
    expect(bandClasses("low")).toContain("surface-200");
  });

  it("formats confidence as a percentage", () => {
    expect(confidencePercent(0.7616)).toBe("76%");
    expect(confidencePercent(1.2)).toBe("100%");
    expect(confidencePercent(-0.1)).toBe("0%");
    expect(confidencePercent(null)).toBe("—");
    expect(bandLabel("high", 0.7616)).toBe("high · 76%");
  });
});

describe("timelineText", () => {
  it("shows score and frames in order", () => {
    expect(timelineText({ timeline_score: 0.9933, consistent_frames: 9, ordered_frames: 9 })).toBe(
      "timeline 0.99 · 9/9 frames in order",
    );
    expect(timelineText({ timeline_score: 0.5, consistent_frames: 2, ordered_frames: null })).toBe(
      "timeline 0.50 · 2 frames in order",
    );
  });

  it("says when frames have no order", () => {
    expect(timelineText({ timeline_score: null, consistent_frames: 0, ordered_frames: null })).toBe(
      "no frame order (poster only)",
    );
  });
});

describe("evidenceNotes", () => {
  it("notes loose and common frames", () => {
    expect(evidenceNotes({ loose_frames: 0, common_frames: 0, common_seconds: [] })).toEqual([]);
    const notes = evidenceNotes({ loose_frames: 4, common_frames: 1, common_seconds: ["00:03"] });
    expect(notes).toHaveLength(2);
    expect(notes[0]).toContain("4 loose frames");
    expect(notes[1]).toContain("1 common frame at 00:03 discounted");
  });
});

describe("signal breakdown", () => {
  const signals = [
    { key: "evidence", label: "Evidence frames", value: "6 evidence frames", weight: 0.45, contribution: 0.443 },
    { key: "common", label: "Common frames", value: "1 common frame", weight: -0.15, contribution: -0.05 },
    { key: "novel", label: "Novel", value: "", weight: null, contribution: 0 },
  ];

  it("formats contributions with a sign", () => {
    expect(formatContribution(0.443)).toBe("+0.44");
    expect(formatContribution(-0.05)).toBe("−0.05");
    expect(formatContribution(0.001)).toBe("0.00");
  });

  it("builds readable rows", () => {
    const rows = signalBreakdown(signals);
    expect(rows[0]).toEqual({
      key: "evidence",
      label: "Evidence frames",
      value: "6 evidence frames",
      contribution: "+0.44",
      weight: "of max 0.45",
      share: expect.closeTo(0.984, 3) as number,
    });
    expect(rows[1]?.weight).toBe("penalty up to 0.15");
    expect(rows[1]?.share).toBeCloseTo(0.333, 3);
    expect(rows[2]).toMatchObject({ weight: "", share: null, contribution: "0.00" });
    expect(signalTotal(signals)).toBe(0.393);
  });

  it("describes thresholds", () => {
    expect(thresholdsText({ high: 0.75, review: 0.4, calibrated: false })).toBe(
      "high ≥ 0.75 (and ≥ 2 evidence frames) · review ≥ 0.40 · defaults",
    );
  });
});

describe("band filter", () => {
  it("parses URL values", () => {
    expect(parseBands(["review", "high", "bogus"])).toEqual(["high", "review"]);
    expect(parseBands("low,high")).toEqual(["high", "low"]);
    expect(parseBands([])).toBeUndefined();
    expect(parseBands(undefined)).toBeUndefined();
    expect(parseDecidedBy("auto")).toBe("auto");
    expect(parseDecidedBy("robot")).toBeUndefined();
  });

  it("toggles chips", () => {
    expect(toggleBand(undefined, "low")).toEqual(["low"]);
    expect(toggleBand(["low"], "high")).toEqual(["high", "low"]);
    expect(toggleBand(["low"], "low")).toBeUndefined();
  });

  it("cycles with B: default → high → review → low → default", () => {
    expect(cycleBands(undefined)).toEqual(["high"]);
    expect(cycleBands(["high"])).toEqual(["review"]);
    expect(cycleBands(["review"])).toEqual(["low"]);
    expect(cycleBands(["low"])).toBeUndefined();
    expect(cycleBands(["high", "low"])).toEqual(["high"]);
  });

  it("describes the filter", () => {
    expect(bandFilterText(undefined, false)).toBe("high + review (low hidden)");
    expect(bandFilterText(undefined, true)).toBe("all bands");
    expect(bandFilterText(["high"], false)).toBe("bands: high");
  });
});
