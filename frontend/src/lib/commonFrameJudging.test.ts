import { describe, expect, it } from "vitest";

import type { MemberFrame, MemberPage } from "./commonFrameJudging";
import {
  ALL_GATED_READING,
  allGatedFactLines,
  memberFrameLabel,
  notShowableReason,
  showableMemberFrame,
  summarizeAllGated,
} from "./commonFrameJudging";

function frame(overrides: Partial<MemberFrame> = {}): MemberFrame {
  return {
    media_id: 1,
    frame_index: 0,
    position: null,
    timestamp_s: null,
    distance: 3,
    showable: true,
    kind: "frame",
    ...overrides,
  };
}

function page(overrides: Partial<MemberPage> = {}): MemberPage {
  return {
    page: {
      id: 1,
      site: "site.example",
      title: "",
      url: "https://site.example/x",
      duration_s: null,
      availability: "unknown",
      availability_checked_at: null,
      uploaded_at: null,
      safety_status: "clear",
    },
    media: [],
    media_gated: true,
    member_frames: [],
    frame_count: 0,
    ...overrides,
  };
}

describe("memberFrameLabel", () => {
  it("says frame N of M at mm:ss for an ordered, timed frame", () => {
    const label = memberFrameLabel(frame({ position: 3, timestamp_s: 2, distance: 5 }), 8);
    expect(label).toBe("frame 3 of 8 at 00:02 · distance 5 bits");
  });

  it("drops the timestamp when none is known", () => {
    const label = memberFrameLabel(frame({ position: 3, timestamp_s: null, distance: 1 }), 8);
    expect(label).toBe("frame 3 of 8 · distance 1 bit");
  });

  it("says only the distance for an unordered (poster-shaped) member", () => {
    const label = memberFrameLabel(frame({ position: null, distance: 0 }), 5);
    expect(label).toBe("this page's repeated frame · distance 0 bits");
  });
});

describe("notShowableReason", () => {
  it("names the multi-frame media kinds", () => {
    expect(notShowableReason("sprite")).toMatch(/sprite sheet/);
    expect(notShowableReason("preview")).toMatch(/preview clip/);
    expect(notShowableReason("capture")).toMatch(/browser capture/);
  });

  it("falls back to a generic reason for an unknown kind", () => {
    expect(notShowableReason("something_new")).toMatch(/more than one frame/);
  });
});

describe("showableMemberFrame", () => {
  it("returns the first showable member frame", () => {
    const p = page({
      member_frames: [
        frame({ media_id: 1, showable: false, kind: "preview" }),
        frame({ media_id: 2, showable: true, kind: "frame" }),
      ],
    });
    expect(showableMemberFrame(p)?.media_id).toBe(2);
  });

  it("returns null when nothing is showable", () => {
    const p = page({ member_frames: [frame({ showable: false, kind: "sprite" })] });
    expect(showableMemberFrame(p)).toBeNull();
  });
});

describe("summarizeAllGated", () => {
  it("counts durations, distinct titles, frame counts and sites without naming a title", () => {
    const pages = [
      page({
        page: { ...page().page, id: 1, title: "one", site: "a.example", duration_s: 12 },
        frame_count: 1,
      }),
      page({
        page: { ...page().page, id: 2, title: "one", site: "a.example", duration_s: 12 },
        frame_count: 1,
      }),
      page({
        page: { ...page().page, id: 3, title: "two", site: "b.example", duration_s: 45 },
        frame_count: 2,
      }),
    ];

    const facts = summarizeAllGated(pages);

    expect(facts.pageCount).toBe(3);
    expect(facts.distinctTitleCount).toBe(2);
    expect(facts.siteCount).toBe(2);
    expect(facts.durations).toEqual([
      { value: 12, count: 2 },
      { value: 45, count: 1 },
    ]);
    expect(facts.frameCounts).toEqual([
      { value: 1, count: 2 },
      { value: 2, count: 1 },
    ]);
    const lines = allGatedFactLines(facts);
    expect(lines.join(" ")).not.toContain("one");
    expect(lines.join(" ")).not.toContain("two");
    expect(lines.some((line) => line.includes("Distinct titles: 2"))).toBe(true);
  });

  it("treats a missing duration as its own bucket, never as zero", () => {
    const facts = summarizeAllGated([page({ page: { ...page().page, duration_s: null } })]);
    expect(facts.durations).toEqual([{ value: null, count: 1 }]);
    expect(allGatedFactLines(facts).some((line) => line.includes("no duration stored"))).toBe(
      true,
    );
  });
});

it("states the reading in plain words, once", () => {
  expect(ALL_GATED_READING.length).toBeGreaterThan(20);
});
