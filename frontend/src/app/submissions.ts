/**
 * Uploads that are being added (ADR 0028 write path), pure and tested.
 *
 * On production an original sent from "Add an original" is accepted first (202) and added a few
 * minutes later, once development has processed it. Until then the overview shows it as "being
 * added", and afterwards either the original itself or why it was not added. The API sends a status
 * and a code; the words live in `src/i18n` under `addOriginal.submissions` and reuse the upload
 * errors' sentences for the same refusal codes. Kept apart from `addOriginal.ts` so the overview
 * does not pull the add page's helpers into the app's first chunk.
 */
import type { Schemas } from "./apiTypes";

export type Submission = Schemas["SubmissionOut"];
export type AddOriginalAdded = Schemas["AddOriginalOut"];
export type AddOriginalAccepted = Schemas["AddOriginalAcceptedOut"];

/** A 202 answer: a submission that is being added, not the original. */
export function isAccepted(answer: AddOriginalAdded | AddOriginalAccepted): answer is AddOriginalAccepted {
  return "submission" in answer;
}

/** What the overview lists: uploads still being added, and those that were not added. An added
 * upload is simply an original in the list below it. */
export function openSubmissions(submissions: readonly Submission[]): Submission[] {
  return submissions.filter((submission) => submission.status !== "done");
}

export function anyProcessing(submissions: readonly Submission[] | undefined): boolean {
  return (submissions ?? []).some((submission) => submission.status === "processing");
}

/** Whether an upload that was being added before is settled now: the originals list is stale. */
export function settledSince(before: readonly Submission[] | undefined, after: readonly Submission[]): boolean {
  const processing = new Set((before ?? []).filter((s) => s.status === "processing").map((s) => s.id));
  return after.some((submission) => processing.has(submission.id) && submission.status !== "processing");
}

/** Refusal codes whose words the upload errors already have; any other refusal is "not accepted". */
const SUBMISSION_REFUSALS = new Set(["claim_needs_review", "rights_confirmation_required", "too_long", "no_organisation"]);

/** The sentence under one submission: a translation key, never server text. */
export function submissionMessage(submission: Submission): string {
  switch (submission.status) {
    case "processing":
      return "addOriginal.submissions.processing";
    case "done":
      return "addOriginal.submissions.done";
    case "failed":
      return "addOriginal.submissions.failed";
    case "refused":
      return SUBMISSION_REFUSALS.has(submission.code)
        ? `addOriginal.errors.server.${submission.code}`
        : "addOriginal.errors.server.invalid";
  }
}

export const SUBMISSION_STATUSES = ["processing", "done", "refused", "failed"] as const;

/** Every key a submission can be worded with, for the test that requires both languages. */
export const SUBMISSION_KEYS: readonly string[] = [
  "addOriginal.submissions.heading",
  ...SUBMISSION_STATUSES.map((status) => `addOriginal.submissions.status.${status}`),
  "addOriginal.submissions.processing",
  "addOriginal.submissions.done",
  "addOriginal.submissions.failed",
  ...[...SUBMISSION_REFUSALS, "invalid"].map((code) => `addOriginal.errors.server.${code}`),
];
