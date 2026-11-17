import { describe, expect, it } from "vitest";

import { de } from "../i18n/de";
import { en } from "../i18n/en";
import {
  SUBMISSION_KEYS,
  type Submission,
  anyProcessing,
  isAccepted,
  openSubmissions,
  settledSince,
  submissionMessage,
} from "./submissions";

function submission(overrides: Partial<Submission> = {}): Submission {
  return {
    id: `sub_${"1".repeat(32)}`,
    title: "Beach day",
    status: "processing",
    code: "",
    original_id: null,
    received_at: "2026-09-17T10:00:00Z",
    ...overrides,
  };
}

function lookup(resources: unknown, key: string): string | undefined {
  let node: unknown = resources;
  for (const part of key.split(".")) {
    if (!node || typeof node !== "object") return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : undefined;
}

describe("submissions", () => {
  it("tells a 202 answer from an added original", () => {
    expect(isAccepted({ submission: submission() })).toBe(true);
    expect(
      isAccepted({
        created: true,
        original: {} as never,
      }),
    ).toBe(false);
  });

  it("lists what is being added or was not added, never what already shows as an original", () => {
    const processing = submission();
    const done = submission({ id: `sub_${"2".repeat(32)}`, status: "done", code: "created" });
    const refused = submission({ id: `sub_${"3".repeat(32)}`, status: "refused", code: "claim_needs_review" });

    expect(openSubmissions([processing, done, refused])).toEqual([processing, refused]);
    expect(anyProcessing([done, refused])).toBe(false);
    expect(anyProcessing([done, processing])).toBe(true);
    expect(anyProcessing(undefined)).toBe(false);
  });

  it("notices when an upload that was being added settled", () => {
    const before = [submission()];

    expect(settledSince(before, [submission()])).toBe(false);
    expect(settledSince(before, [submission({ status: "done", code: "created" })])).toBe(true);
    expect(settledSince(before, [submission({ status: "failed", code: "processing_failed" })])).toBe(true);
    expect(settledSince(undefined, [submission({ status: "done" })])).toBe(false);
  });

  it("words each outcome with the upload's own sentences, never server text", () => {
    expect(submissionMessage(submission())).toBe("addOriginal.submissions.processing");
    expect(submissionMessage(submission({ status: "failed", code: "processing_failed" }))).toBe(
      "addOriginal.submissions.failed",
    );
    expect(submissionMessage(submission({ status: "refused", code: "claim_needs_review" }))).toBe(
      "addOriginal.errors.server.claim_needs_review",
    );
    expect(submissionMessage(submission({ status: "refused", code: "unsupported_fingerprint" }))).toBe(
      "addOriginal.errors.server.invalid",
    );
  });

  it("has words for every status and outcome in both languages", () => {
    for (const key of SUBMISSION_KEYS) {
      for (const resources of [en, de]) {
        expect(lookup(resources, key), key).toBeTruthy();
      }
    }
  });
});
