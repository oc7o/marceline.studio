import { describe, expect, it } from "vitest";

import { LEGEND_NOTES, STATUS_LEGEND, evidenceLabel, linkEvidence, linkedOriginalSummary, provenanceLabel } from "./provenance";

const member = (status: string, provenance: string, evidence: string, serverStatus = status) => ({
  status,
  serverStatus,
  provenance,
  evidence,
});

describe("provenanceLabel", () => {
  it("names the system evidence of proposals", () => {
    expect(provenanceLabel(member("proposed", "system", "pdq"))).toBe("Proposed by system — PDQ");
    expect(provenanceLabel(member("proposed", "system", "sha256"))).toBe("Proposed by system — SHA256");
    expect(provenanceLabel(member("proposed", "system", "external_link"))).toBe("Proposed by system — external link");
    expect(provenanceLabel(member("proposed", "system", "mirror"))).toBe("Proposed by system — mirror poster");
  });

  it("separates pair-label confirmations from manual status changes", () => {
    expect(provenanceLabel(member("confirmed", "pair_label", "manual"))).toBe("Confirmed — from your pair labels");
    expect(provenanceLabel(member("confirmed", "reviewer", "manual"))).toBe("Confirmed — by you (added manually)");
    expect(provenanceLabel(member("confirmed", "reviewer", "pdq"))).toBe("Confirmed — by you (system proposed via PDQ)");
    expect(provenanceLabel(member("rejected", "reviewer", "mirror"))).toBe(
      "Rejected — by you (system proposed via mirror poster)",
    );
  });

  it("marks decisions made in this session", () => {
    expect(provenanceLabel(member("confirmed", "system", "pdq", "proposed"))).toBe("Confirmed — by you (this session)");
    expect(provenanceLabel(member("proposed", "pair_label", "manual", "confirmed"))).toBe(
      "Proposed — reset by you (this session)",
    );
  });

  it("keeps unknown evidence readable", () => {
    expect(evidenceLabel("new_kind")).toBe("new_kind");
  });
});

describe("legend", () => {
  it("explains every status and that nothing is auto-confirmed", () => {
    expect(STATUS_LEGEND.map((entry) => entry.status)).toEqual(["proposed", "confirmed", "rejected"]);
    expect(LEGEND_NOTES[0]).toContain("Nothing is auto-confirmed");
    expect(LEGEND_NOTES.join(" ")).toContain("derived");
  });
});

describe("linked originals", () => {
  const reference = { creator_names: ["Thea Lindqvist"], title: "Winter Drive" };
  const strong = { page_id: 48, status: "proposed", match_strength: "strong", leak_status: null, leak_source: null };
  const leak = { page_id: 19, status: "proposed", match_strength: null, leak_status: "proposed", leak_source: "cluster" };

  it("summarizes the best evidence first", () => {
    expect(linkedOriginalSummary({ reference, pages: [strong] })).toBe(
      "Thea Lindqvist — Winter Drive (strong match on page 48)",
    );
    expect(linkedOriginalSummary({ reference, pages: [strong, leak, { ...leak, page_id: 29 }] })).toBe(
      "Thea Lindqvist — Winter Drive (strong match on page 48, +2 more pages)",
    );
  });

  it("names every creator of an original", () => {
    const shared = { creator_names: ["Sasha Meyer", "Carla Bremer"], title: "Studio Session" };
    expect(linkedOriginalSummary({ reference: shared, pages: [strong] })).toBe(
      "Sasha Meyer + Carla Bremer — Studio Session (strong match on page 48)",
    );
  });

  it("describes member links", () => {
    expect(linkEvidence(leak)).toBe("proposed leak via cluster");
    expect(linkEvidence({ ...strong, status: "confirmed", match_strength: "weak" })).toBe("confirmed · weak match");
    expect(linkEvidence({ ...leak, status: "confirmed", leak_status: "confirmed", leak_source: "manual" })).toBe(
      "confirmed · leak (manual)",
    );
    expect(linkEvidence({ ...leak, status: "rejected" })).toBe("rejected: not a leak");
  });

  it("adds a determined copy extent to match links (issue #62)", () => {
    expect(linkEvidence({ ...strong, copy_extent: "excerpt" })).toBe("strong match · excerpt");
    expect(linkEvidence({ ...strong, status: "confirmed", copy_extent: "full_copy" })).toBe(
      "confirmed · strong match · full copy",
    );
    expect(linkEvidence({ ...strong, copy_extent: "unknown" })).toBe("strong match");
    expect(linkedOriginalSummary({ reference, pages: [{ ...strong, copy_extent: "compilation_candidate" }] })).toBe(
      "Thea Lindqvist — Winter Drive (strong match · possible compilation on page 48)",
    );
  });
});
