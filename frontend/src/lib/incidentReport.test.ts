import { describe, expect, it } from "vitest";

import { draftFor, evidenceLabel, fullText, mediaFetchLabel, reportedBody, toLocalInput } from "./incidentReport";

const DRAFTS = [
  { language: "de" as const, subject: "Betreff", body: "Guten Tag,\n" },
  { language: "en" as const, subject: "Subject", body: "Hello,\n" },
];

describe("draftFor / fullText", () => {
  it("picks the language and joins subject and body", () => {
    const german = draftFor(DRAFTS, "de");
    expect(german?.subject).toBe("Betreff");
    expect(draftFor(DRAFTS, "en")?.body).toBe("Hello,\n");
    expect(draftFor([], "de")).toBeNull();
    expect(german ? fullText(german) : "").toBe("Betreff\n\nGuten Tag,\n");
  });
});

describe("labels", () => {
  it("explains every media fetch state and evidence kind the API sends", () => {
    for (const state of ["none", "fetched", "unknown"]) {
      expect(mediaFetchLabel(state)).not.toContain("status:");
    }
    expect(mediaFetchLabel("later")).toBe("Media fetch status: later");
    for (const evidence of ["origin", "same_url", "term_change", "mirror", "sha256", "cluster", "pdq", "manual"]) {
      expect(evidenceLabel(evidence)).not.toBe(evidence);
    }
    expect(evidenceLabel("new_kind")).toBe("new_kind");
  });
});

describe("reportedBody", () => {
  it("trims fields and sends the local date as an ISO instant", () => {
    const local = new Date(2026, 8, 17, 9, 5);
    const body = reportedBody({
      reference: "  ECO-1 ",
      via: " form ",
      reportCategory: " cat ",
      reportedAt: toLocalInput(local),
      language: "en",
    });
    expect(body).toEqual({
      reference: "ECO-1",
      via: "form",
      report_category: "cat",
      reported_at: local.toISOString(),
      language: "en",
    });
  });

  it("sends no date when the field is empty (the server uses now) and refuses a broken one", () => {
    const base = { reference: "", via: "", reportCategory: "", language: "de" as const };
    expect(reportedBody({ ...base, reportedAt: "" })?.reported_at).toBeNull();
    expect(reportedBody({ ...base, reportedAt: "not a date" })).toBeNull();
  });
});
