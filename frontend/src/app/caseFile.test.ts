/**
 * The case file's pure logic: timeline per copy, evidence, options, hosting, overview order and
 * removals standing — and that every code it can produce has words in the resources.
 */
import { describe, expect, it } from "vitest";

import { de } from "../i18n/de";
import { en } from "../i18n/en";
import {
  ESCALATION_LADDER,
  EVIDENCE_BASES,
  COPY_EXTENTS,
  ABUSE_SOURCES,
  JURISDICTIONS,
  LOOKUP_STATES,
  MONITORED_SOURCES,
  OPTION_ORDER,
  STEP_ACTOR,
  STEP_ORDER,
  REMOVAL_TIMES,
  caseOptions,
  confirmedSites,
  evidenceOf,
  extentExplanationKey,
  countryName,
  hostsOf,
  leakTimeline,
  parsePublicId,
  removalStanding,
  sortOriginals,
  stateCounts,
  whereabouts,
  type Host,
} from "./caseFile";
import type { CaseStep, CaseUnknown, CustomerFinding } from "./queries";

// Public ids as the API sends them (ADR 0033).
const ORIGINAL = "orig_" + "7".repeat(32);
const OTHER_ORIGINAL = "orig_" + "8".repeat(32);
const PAGE = "page_" + "1".repeat(32);

const CASE_TIMELINE: CaseStep[] = [
  { step: "confirmed", status: "done", at: null, on: "2026-09-02", reason: null, blocked_by_issue: null },
  { step: "notice_prepared", status: "pending", at: null, on: null, reason: null, blocked_by_issue: null },
  { step: "notice_sent", status: "pending", at: null, on: null, reason: null, blocked_by_issue: null },
  { step: "notice_acknowledged", status: "pending", at: null, on: null, reason: null, blocked_by_issue: null },
  { step: "removed", status: "pending", at: null, on: null, reason: null, blocked_by_issue: null },
  { step: "rechecked", status: "done", at: "2026-09-15T08:00:00Z", on: null, reason: null, blocked_by_issue: null },
];

const NOT_AVAILABLE: CaseUnknown[] = [
  { topic: "notice_delivery", reason: "legal_review_pending", blocked_by_issue: 46 },
  { topic: "reupload_watch_coverage", reason: "capability_not_built", blocked_by_issue: null },
];

function host(hostname: string): Host {
  return {
    hostname,
    lookup: "not_looked_up",
    looked_up_at: null,
    stale: false,
    addresses: [],
    asn: null,
    as_name: null,
    provider: null,
    country: null,
    jurisdiction: "unknown",
    cdn: null,
    abuse_contact: null,
    registrar: null,
    response_history: null,
  };
}

function finding(overrides: Partial<CustomerFinding> = {}): CustomerFinding {
  return {
    original_id: ORIGINAL,
    page_id: PAGE,
    site: "video.example",
    url: "https://video.example/video/101",
    case_state: "open",
    availability: "playable",
    online: true,
    last_checked: "2026-09-14T09:00:00Z",
    confirmed_on: "2026-09-02",
    removed_at: null,
    reappeared_at: null,
    evidence: "fingerprint_match",
    extent: "full_copy",
    hosting: { page_host: host("video.example"), content_hosts: [] },
    ...overrides,
  };
}

describe("leakTimeline", () => {
  it("always has the six steps, in order", () => {
    expect(STEP_ORDER).toEqual(["confirmed", "notice_prepared", "notice_sent", "notice_acknowledged", "removed", "rechecked"]);
    expect(leakTimeline(finding(), CASE_TIMELINE).map((entry) => entry.step)).toEqual([...STEP_ORDER]);
    expect(leakTimeline(finding(), []).map((entry) => entry.step)).toEqual([...STEP_ORDER]);
  });

  it("dates our own step to the day, and the external ones to the moment", () => {
    const steps = leakTimeline(finding(), CASE_TIMELINE);
    // Found and verified are one step, with a day and no time (operator decision, 2026-09-17).
    expect(steps[0]).toEqual({
      step: "confirmed",
      status: "done",
      at: null,
      on: "2026-09-02",
      reason: null,
      actor: "us",
      reappearedAt: null,
    });
    expect(steps[4]).toMatchObject({ step: "removed", status: "pending", at: null, actor: "site" });
    // The copy's last check, not the case's latest check of any copy.
    expect(steps[5]).toMatchObject({ step: "rechecked", status: "done", at: "2026-09-14T09:00:00Z", on: null });
  });

  it("renders the notice steps verbatim from the API, actor attributed (issue #33: real now)", () => {
    const notices = leakTimeline(finding(), CASE_TIMELINE).slice(1, 4);
    expect(notices).toEqual([
      expect.objectContaining({ step: "notice_prepared", status: "pending", at: null, reason: null, actor: "us" }),
      expect.objectContaining({ step: "notice_sent", status: "pending", at: null, reason: null, actor: "you" }),
      expect.objectContaining({ step: "notice_acknowledged", status: "pending", at: null, reason: null, actor: "site" }),
    ]);
  });

  it("never turns a step the API did not send into an upcoming one", () => {
    const steps = leakTimeline(finding(), []);
    expect(steps[1]).toMatchObject({ status: "unavailable", reason: null });
  });

  it("leaves an unchecked copy's re-check pending — no date invented", () => {
    const steps = leakTimeline(finding({ last_checked: null }), CASE_TIMELINE);
    expect(steps[5]).toMatchObject({ step: "rechecked", status: "pending", at: null });
  });

  it("marks a removal that came back", () => {
    const steps = leakTimeline(
      finding({ removed_at: "2026-09-05T00:00:00Z", reappeared_at: "2026-09-10T00:00:00Z", online: true }),
      CASE_TIMELINE,
    );
    expect(steps[4]).toMatchObject({
      step: "removed",
      status: "done",
      at: "2026-09-05T00:00:00Z",
      reappearedAt: "2026-09-10T00:00:00Z",
    });
  });
});

describe("whereabouts", () => {
  it("has three answers", () => {
    expect(whereabouts({ online: true })).toBe("online");
    expect(whereabouts({ online: false })).toBe("gone");
    expect(whereabouts({ online: null })).toBe("unknown");
  });
});

describe("evidenceOf", () => {
  it("says the fingerprints match and the day it was confirmed — never how sure we are", () => {
    expect(evidenceOf(finding())).toEqual([{ kind: "fingerprint_match" }, { kind: "confirmed", on: "2026-09-02" }]);
  });

  it("says when there is no fingerprint match", () => {
    expect(evidenceOf(finding({ evidence: "no_fingerprint_match" }))).toEqual([
      { kind: "no_fingerprint_match" },
      { kind: "confirmed", on: "2026-09-02" },
    ]);
  });
});

describe("options and hosting", () => {
  it("lists every option, each with the API's reason or none", () => {
    expect(caseOptions(NOT_AVAILABLE)).toEqual([
      { option: "prepare_notice", reason: null },
      { option: "search_delisting", reason: null },
      { option: "escalation", reason: null },
      { option: "reupload_watch", reason: "capability_not_built" },
    ]);
  });

  it("lists the page host first and each preview host once", () => {
    const hosts = hostsOf(
      finding({
        hosting: {
          page_host: host("video.example"),
          content_hosts: [host("cdn.example"), host("video.example"), host("cdn.example"), host("img.example")],
        },
      }),
    );
    expect(hosts.map(({ role, host: row }) => `${role}:${row.hostname}`)).toEqual([
      "page:video.example",
      "content:cdn.example",
      "content:img.example",
    ]);
  });

  it("names countries in the reader's language, and falls back to the code", () => {
    expect(countryName("de", "en")).toBe("Germany");
    expect(countryName("DE", "de")).toBe("Deutschland");
    expect(countryName("not a code", "en")).toBe("not a code");
  });
});

describe("overview helpers", () => {
  it("puts online copies first and keeps the API's order within a state", () => {
    const originals = [
      { id: 1, state: "nothing_found" as const },
      { id: 2, state: "found_removed" as const },
      { id: 3, state: "found_online" as const },
      { id: 4, state: "found_online" as const },
    ];
    expect(sortOriginals(originals).map((row) => row.id)).toEqual([3, 4, 2, 1]);
    expect(stateCounts(originals)).toEqual({ found_online: 2, found_removed: 1, nothing_found: 1 });
  });

  it("lists the sites of one original's copies, once each", () => {
    const findings = [
      finding({ site: "a.example" }),
      finding({ site: "a.example", page_id: "page_" + "2".repeat(32) }),
      finding({ site: "b.example", page_id: "page_" + "3".repeat(32) }),
      finding({ site: "c.example", original_id: OTHER_ORIGINAL }),
    ];
    expect(confirmedSites(findings, ORIGINAL)).toEqual(["a.example", "b.example"]);
  });
});

describe("removalStanding", () => {
  it("is handled only when removed and the case stayed removed", () => {
    expect(removalStanding({ state: "removed", handled: true })).toBe("handled");
    expect(removalStanding({ state: "removed", handled: false })).toBe("removed_case_open");
    expect(removalStanding({ state: "reopened", handled: false })).toBe("reopened");
  });
});

describe("parsePublicId", () => {
  it("accepts a public id of the expected kind only", () => {
    expect(parsePublicId("orig", ORIGINAL)).toBe(ORIGINAL);
    expect(parsePublicId("page", PAGE)).toBe(PAGE);
    const bad = [
      undefined,
      "",
      "42", // an old integer URL
      "0",
      "abc",
      PAGE, // another kind
      ORIGINAL.toUpperCase(),
      ORIGINAL.slice(0, -1),
      `${ORIGINAL}0`,
      `${ORIGINAL}/`,
      ` ${ORIGINAL}`,
      "orig_" + "g".repeat(32),
    ];
    for (const value of bad) {
      expect(parsePublicId("orig", value)).toBeNull();
    }
  });
});

/** Every code the logic can produce has words, in both languages (the dynamic `t()` keys). */
describe("words for every code", () => {
  const lookup = (resources: unknown, path: string): unknown =>
    path.split(".").reduce<unknown>((node, key) => (node as Record<string, unknown> | undefined)?.[key], resources);

  const keys = [
    ...STEP_ORDER.map((step) => `caseFile.timeline.steps.${step}`),
    ...(["done", "pending", "unavailable"] as const).map((status) => `caseFile.timeline.status.${status}`),
    ...Object.values(STEP_ACTOR).map((actor) => `caseFile.actors.${actor}`),
    ...(["capability_not_built", "legal_review_pending", "prerequisite_unavailable"] as const).map(
      (reason) => `caseFile.reasons.${reason}`,
    ),
    ...EVIDENCE_BASES.map((basis) => (basis === "fingerprint_match" ? "caseFile.why.fingerprintMatch" : "caseFile.why.noMatch")),
    ...COPY_EXTENTS.flatMap((extent) => [`finding.extent.${extent}.label`, `finding.extent.${extent}.explanation`]),
    // The one extent a photo collection can show, in words of its own (issue #116, ADR 0053).
    ...COPY_EXTENTS.map((extent) => extentExplanationKey(extent, "photos")),
    ...JURISDICTIONS.map((jurisdiction) => `caseFile.hosting.jurisdiction.${jurisdiction}`),
    ...LOOKUP_STATES.filter((state) => state !== "complete").map((state) => `caseFile.hosting.lookup.${state}`),
    ...ABUSE_SOURCES.map((source) => `caseFile.hosting.abuseSource.${source}`),
    ...REMOVAL_TIMES.map((time) => `caseFile.hosting.history.usually.${time}`),
    ...MONITORED_SOURCES.map((bucket) => `coverage.sources.${bucket}`),
    ...OPTION_ORDER.flatMap((option) => [`caseFile.options.${option}.label`, `caseFile.options.${option}.body`]),
    ...ESCALATION_LADDER.map((rung) => `caseFile.options.ladder.${rung}`),
    ...(["online", "gone", "unknown"] as const).map((where) => `finding.whereabouts.${where}`),
    ...(["nothing_found", "found_online", "found_removed"] as const).flatMap((state) => [
      `states.${state}`,
      `overview.summary.${state}_one`,
      `overview.summary.${state}_other`,
    ]),
    ...(["handled", "removed_case_open", "reopened"] as const).map((standing) => `removals.standing.${standing}`),
  ];

  it.each([
    ["en", en],
    ["de", de],
  ])("%s has them all", (_language, resources) => {
    expect(keys.filter((key) => typeof lookup(resources, key) !== "string")).toEqual([]);
  });
});
