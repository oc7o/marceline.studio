/**
 * The overview's catalogue-wide aggregation: totals, the most recent changes across every
 * original, and which originals with a finding still have a rights step in the way.
 */
import { describe, expect, it } from "vitest";

import { catalogueTotals, recentChanges, rightsAttentionFor } from "./dashboard";
import type { CustomerFinding, CustomerOriginal, Removal } from "./queries";
import type { RightsWork } from "./rights";

const ORIGINAL_A = "orig_" + "1".repeat(32);
const ORIGINAL_B = "orig_" + "2".repeat(32);
const ORIGINAL_C = "orig_" + "3".repeat(32);
const PAGE_A = "page_" + "1".repeat(32);
const PAGE_B = "page_" + "2".repeat(32);

function original(overrides: Partial<CustomerOriginal> = {}): CustomerOriginal {
  return {
    id: ORIGINAL_A,
    title: "Beach day",
    kind: "video",
    duration_s: 600,
    created_at: "2026-08-01T09:00:00Z",
    state: "found_online",
    counts: { confirmed: 2, removed: 1 },
    ...overrides,
  };
}

function finding(overrides: Partial<CustomerFinding> = {}): CustomerFinding {
  return {
    original_id: ORIGINAL_A,
    page_id: PAGE_A,
    site: "video.example",
    url: "https://video.example/video/1",
    case_state: "open",
    availability: "playable",
    online: true,
    last_checked: "2026-09-14T09:00:00Z",
    confirmed_on: "2026-09-02",
    removed_at: null,
    reappeared_at: null,
    evidence: "fingerprint_match",
    extent: "full_copy",
    hosting: {
      page_host: { hostname: "video.example", lookup: "not_looked_up", looked_up_at: null, stale: false, addresses: [], asn: null, as_name: null, provider: null, country: null, jurisdiction: "unknown", cdn: null, abuse_contact: null, registrar: null, response_history: null },
      content_hosts: [],
    },
    ...overrides,
  };
}

function removal(overrides: Partial<Removal> = {}): Removal {
  return {
    original_id: ORIGINAL_A,
    title: "Beach day",
    page_id: PAGE_B,
    site: "mirror.example",
    url: "https://mirror.example/v/2",
    removed_at: "2026-09-10T07:00:00Z",
    confirmed_on: "2026-09-02",
    reappeared_at: null,
    last_checked: "2026-09-15T08:00:00Z",
    state: "removed",
    case_state: "removed_watching",
    handled: true,
    ...overrides,
  };
}

function work(overrides: Partial<RightsWork> = {}): RightsWork {
  return {
    original_id: ORIGINAL_A,
    title: "Beach day",
    rights_status: "unverified",
    capacity: null,
    underlying_owner_name: null,
    asserted_right: null,
    asserted_right_detail: "",
    declared_at: null,
    declaration_version: null,
    authority_declared_at: null,
    authority_declaration_version: null,
    notice_blockers: [],
    ...overrides,
  };
}

describe("catalogueTotals", () => {
  it("sums originals and their counts, without recomputing what a page's state is", () => {
    const totals = catalogueTotals([
      original({ counts: { confirmed: 2, removed: 1 } }),
      original({ id: ORIGINAL_B, counts: { confirmed: 0, removed: 0 } }),
      original({ id: ORIGINAL_C, counts: { confirmed: 3, removed: 3 } }),
    ]);
    expect(totals).toEqual({ originals: 3, confirmed: 5, removed: 4 });
  });

  it("is zero for an empty catalogue", () => {
    expect(catalogueTotals([])).toEqual({ originals: 0, confirmed: 0, removed: 0 });
  });
});

describe("recentChanges", () => {
  const originals = [
    original({ id: ORIGINAL_A, title: "Beach day" }),
    original({ id: ORIGINAL_B, title: "Studio session" }),
  ];

  it("merges findings and removals into one feed, newest first", () => {
    const changes = recentChanges(
      originals,
      [finding({ original_id: ORIGINAL_A, confirmed_on: "2026-09-02" })],
      [
        removal({ original_id: ORIGINAL_A, title: "Beach day", removed_at: "2026-09-10T07:00:00Z" }),
        removal({
          original_id: ORIGINAL_B,
          title: "Studio session",
          page_id: PAGE_A,
          removed_at: "2026-09-05T07:00:00Z",
          reappeared_at: "2026-09-16T07:00:00Z",
          state: "reopened",
        }),
      ],
    );
    expect(changes).toEqual([
      { kind: "reappeared", at: "2026-09-16T07:00:00Z", originalId: ORIGINAL_B, title: "Studio session" },
      { kind: "removed", at: "2026-09-10T07:00:00Z", originalId: ORIGINAL_A, title: "Beach day" },
      { kind: "confirmed", at: "2026-09-02", originalId: ORIGINAL_A, title: "Beach day" },
    ]);
  });

  it("caps the list at the given limit", () => {
    const removals = Array.from({ length: 10 }, (_, index) =>
      removal({
        page_id: `page_${String(index).padStart(32, "0")}`,
        removed_at: `2026-09-${String(index + 1).padStart(2, "0")}T00:00:00Z`,
      }),
    );
    expect(recentChanges(originals, [], removals, 3)).toHaveLength(3);
  });

  it("skips a finding whose original is not in the list, rather than showing a blank title", () => {
    const changes = recentChanges(
      originals,
      [finding({ original_id: "orig_" + "9".repeat(32) })],
      [],
    );
    expect(changes).toEqual([]);
  });

  it("is empty when nothing changed", () => {
    expect(recentChanges(originals, [], [])).toEqual([]);
  });
});

describe("rightsAttentionFor", () => {
  const originals = [
    original({ id: ORIGINAL_A, state: "found_online" }),
    original({ id: ORIGINAL_B, state: "found_removed" }),
    original({ id: ORIGINAL_C, state: "nothing_found" }),
  ];

  it("lists an original with a finding that still has a rights blocker", () => {
    const attention = rightsAttentionFor(originals, [
      work({ original_id: ORIGINAL_A, title: "Beach day", notice_blockers: ["claimant_type_missing"] }),
    ]);
    expect(attention).toEqual([
      { originalId: ORIGINAL_A, title: "Beach day", blockers: ["claimant_type_missing"] },
    ]);
  });

  it("never lists an original nothing was found of, even with blockers on record", () => {
    const attention = rightsAttentionFor(originals, [
      work({ original_id: ORIGINAL_C, title: "Nothing here", notice_blockers: ["claimant_type_missing"] }),
    ]);
    expect(attention).toEqual([]);
  });

  it("says nothing about an original with no rights row at all", () => {
    expect(rightsAttentionFor(originals, [])).toEqual([]);
  });

  it("says nothing once every blocker is cleared", () => {
    const attention = rightsAttentionFor(originals, [
      work({ original_id: ORIGINAL_A, notice_blockers: [] }),
    ]);
    expect(attention).toEqual([]);
  });
});
