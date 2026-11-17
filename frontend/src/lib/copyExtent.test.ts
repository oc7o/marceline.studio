import { describe, expect, it } from "vitest";

import { EXTENT_REASON_CODES, copyExtentLabel, copyExtentText, copyExtentTone, type ExtentInput } from "./copyExtent";

const match = (fields: Partial<ExtentInput>): ExtentInput => ({
  copy_extent: "unknown",
  copy_extent_reason: "no_timeline",
  copy_span_s: null,
  copy_coverage: null,
  copy_other_originals: 0,
  ...fields,
});

describe("copyExtentText", () => {
  it("names the extent, the rule and the span", () => {
    expect(
      copyExtentText(
        match({ copy_extent: "excerpt", copy_extent_reason: "page_shorter", copy_span_s: 101, copy_coverage: 0.028 }),
      ),
    ).toBe("excerpt — page clearly shorter than the original · spans 01:41 (3% of the original)");
    expect(
      copyExtentText(match({ copy_extent: "full_copy", copy_extent_reason: "full_span", copy_span_s: 7176, copy_coverage: 0.979 })),
    ).toBe("full copy — matched frames span the original · spans 119:36 (98% of the original)");
  });

  it("says how many other originals make a page a possible compilation", () => {
    const text = copyExtentText(
      match({ copy_extent: "compilation_candidate", copy_extent_reason: "other_originals", copy_other_originals: 2 }),
    );
    expect(text).toBe("possible compilation — 2 other originals matched on this page");
  });

  it("never claims a span for poster-only evidence", () => {
    expect(copyExtentText(match({}))).toBe(
      "extent unknown — no frame order to measure (poster or fewer than 3 frames in order)",
    );
  });

  it("has words for every reason code", () => {
    expect(EXTENT_REASON_CODES).toHaveLength(11);
    for (const code of EXTENT_REASON_CODES) {
      expect(copyExtentText(match({ copy_extent_reason: code }))).not.toContain("undefined");
    }
  });
});

describe("copyExtentLabel and tone", () => {
  it("marks what needs a licence/exception review before a notice", () => {
    expect(copyExtentLabel("compilation_candidate")).toBe("possible compilation");
    expect(copyExtentTone("full_copy")).toBe("accent");
    expect(copyExtentTone("excerpt")).toBe("caution");
    expect(copyExtentTone("compilation_candidate")).toBe("caution");
    expect(copyExtentTone("unknown")).toBe("neutral");
  });
});
