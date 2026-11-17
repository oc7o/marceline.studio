/**
 * The customer app addresses the creator with "du" (operator decision 2026-09-19), like the
 * public website (`website/README.md`) — a single-audience choice: the moderation SPA has no
 * German resource of its own to guard.
 *
 * This scans every German customer slice for the formal address ("Sie", "Ihr…", "Ihnen") so a
 * string another branch adds in `Sie`-form fails here instead of shipping. Sentence-initial "Sie"
 * / "Ihr" can also be third-person ("sie" = they/she, "ihr" = their, capitalised only because it
 * opens a sentence) — a genuine case like that is named in `ALLOWED_THIRD_PERSON` by its full key
 * path, never by loosening the pattern.
 */
import { describe, expect, it } from "vitest";

import { deAccountEmail } from "./de/accountEmail";
import { deAddOriginal } from "./de/addOriginal";
import { deCaseFile } from "./de/caseFile";
import { deCore } from "./de/core";
import { deDataRequests } from "./de/dataRequests";
import { deNotices } from "./de/notices";
import { deOnboarding } from "./de/onboarding";
import { deRights } from "./de/rights";
import { deVerification } from "./de/verification";

/** Word-boundary: "Sie", "Ihr", "Ihre", "Ihrem", "Ihren", "Ihrer", "Ihnen". */
const FORMAL_ADDRESS = /\b(Sie|Ihre[nmrs]?|Ihr|Ihnen)\b/;

/**
 * Keys where a match is third-person ("sie"/"ihr" = they/their), not the formal "you", and is
 * capitalised only because it opens a sentence. Empty today — nothing in the nine slices needs it —
 * kept so a genuine future case is named here instead of the pattern being weakened.
 */
const ALLOWED_THIRD_PERSON: ReadonlySet<string> = new Set([]);

const SLICES: Record<string, unknown> = {
  core: deCore,
  caseFile: deCaseFile,
  addOriginal: deAddOriginal,
  rights: deRights,
  notices: deNotices,
  onboarding: deOnboarding,
  verification: deVerification,
  dataRequests: deDataRequests,
  accountEmail: deAccountEmail,
};

function strings(value: unknown, prefix = ""): [string, string][] {
  if (typeof value === "string") return [[prefix, value]];
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) =>
      strings(child, prefix ? `${prefix}.${key}` : key),
    );
  }
  return [];
}

describe.each(Object.entries(SLICES))("German customer slice '%s' addresses the reader with du", (name, slice) => {
  it("contains no formal Sie/Ihr/Ihnen address", () => {
    const hits = strings(slice)
      .filter(([key]) => !ALLOWED_THIRD_PERSON.has(`${name}.${key}`))
      .filter(([, text]) => FORMAL_ADDRESS.test(text))
      .map(([key, text]) => `${name}.${key}: "${text}"`);
    expect(hits).toEqual([]);
  });
});
