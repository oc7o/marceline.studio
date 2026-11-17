/**
 * What happens to a file *after* it has been sent (issue #59, ADR 0044 amendment 2026-09-18).
 *
 * The upload itself ends when the last chunk is acknowledged; the fingerprinting begins there, on
 * our server, and used to be a single "queued" line. This module is everything the screens derive
 * from the status the API answers — pure, so the polling schedule, the bar and the words are
 * tested without a browser, a timer or a server.
 *
 * Two rules it exists to keep:
 *
 * - **A null total is not a zero.** Until the file has been probed nobody knows how many moments
 *   it has, so the bar is indeterminate rather than at 0 %, and there is never a time anywhere —
 *   `src/i18n/wording.test.ts` bans a duration promise and this is the screen that would want one.
 * - **The API's `reason` is a closed set and the words are ours.** A code without a sentence would
 *   otherwise reach a customer as an English identifier; `reasonKey` only ever builds a key from
 *   `UPLOAD_FAILURE_REASONS`, and `UPLOAD_STATUS_KEYS` is what the i18n test walks.
 *
 * Kept out of `addOriginal.ts` for the same reason `submissions.ts` is: the overview lists uploads
 * in flight and must not pull the add page's helpers into the app's first chunk.
 */
import type { Schemas } from "./apiTypes";

export type Upload = Schemas["UploadOut"];
export type UploadState = Upload["status"];
export type UploadFailureReason = NonNullable<Upload["reason"]>;

/** Every state the job row can be in, in the order it moves through them. */
export const UPLOAD_STATES: readonly UploadState[] = [
  "receiving",
  "queued",
  "hashing",
  "done",
  "refused",
  "failed",
  "expired",
  "cancelled",
];

/** Nothing moves after these: polling stops, and the screen says what became of the file. */
export const TERMINAL_STATES: readonly UploadState[] = [
  "done",
  "refused",
  "failed",
  "expired",
  "cancelled",
];

/** The closed set the API answers. Every one of them has a sentence in both languages. */
export const UPLOAD_FAILURE_REASONS: readonly UploadFailureReason[] = [
  "unsupported_format",
  "too_long",
  "too_large",
  "decode_failed",
  "timed_out",
  "quota",
  "incomplete",
  "needs_review",
  "cancelled",
  "internal",
];

export function isTerminal(status: UploadState): boolean {
  return TERMINAL_STATES.includes(status);
}

/** Done *and* with an original to link to. A `done` row without one is still not an original. */
export function isAdded(upload: Upload): boolean {
  return upload.status === "done" && Boolean(upload.original_id);
}

/**
 * How long to wait before asking again, by attempt: fast while a short file is likely to finish,
 * then slower, because a long one is minutes of work and nobody is served by 600 requests.
 */
export const POLL_DELAYS_MS: readonly number[] = [1_000, 1_000, 2_000, 3_000, 5_000, 8_000, 15_000];

export function pollDelayMs(attempt: number): number {
  const index = Math.min(Math.max(attempt, 0), POLL_DELAYS_MS.length - 1);
  return POLL_DELAYS_MS[index] ?? POLL_DELAYS_MS[POLL_DELAYS_MS.length - 1] ?? 15_000;
}

/**
 * Whether to ask again at all: not once it is settled, and not while the tab is hidden — the
 * server carries on either way, so a background tab polling for an hour buys nothing.
 */
export function shouldPoll({
  status,
  hidden,
}: {
  status: UploadState | null;
  hidden: boolean;
}): boolean {
  if (status === null) return !hidden;
  return !isTerminal(status) && !hidden;
}

/** The bar, 0..100, or null while the total is unknown — then the bar is indeterminate. */
export function framesPercent(upload: Upload): number | null {
  const total = upload.frames_total;
  if (total === null || total <= 0) return null;
  return Math.max(0, Math.min(100, Math.round((upload.frames_done / total) * 100)));
}

/** The bytes of the upload itself, 0..100. */
export function bytesPercent(upload: Upload): number {
  if (upload.declared_bytes <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((upload.received_bytes / upload.declared_bytes) * 100)));
}

/** The sentence under one upload: a translation key, never anything the server wrote. */
export function statusKey(upload: Upload): string {
  if (upload.reason !== null && upload.reason !== undefined) return reasonKey(upload.reason);
  return `addOriginal.upload.state.${upload.status}`;
}

export function reasonKey(reason: UploadFailureReason): string {
  return UPLOAD_FAILURE_REASONS.includes(reason)
    ? `addOriginal.upload.reasons.${reason}`
    : "addOriginal.upload.reasons.internal";
}

/**
 * Whether the screen may say the file has been deleted. Only a deletion the server checked
 * afterwards sets this; without it the sentence would be a promise rather than a fact.
 */
export function saysDeleted(upload: Upload): boolean {
  return isTerminal(upload.status) && upload.bytes_deleted;
}

/** How long a file that was not added stays on the overview. */
export const RECENT_FAILURE_DAYS = 7;
/** How many uploads the overview lists at once. */
export const OPEN_UPLOADS_LIMIT = 5;

/**
 * What the overview and the add page show when the customer comes back: uploads still in flight,
 * and recent ones that were not added. An added upload is simply an original in the list below,
 * and a cancelled one was the customer's own decision.
 */
export function openUploads(
  uploads: readonly Upload[],
  now: Date = new Date(),
  limit: number = OPEN_UPLOADS_LIMIT,
): Upload[] {
  const since = now.getTime() - RECENT_FAILURE_DAYS * 24 * 60 * 60 * 1000;
  return uploads
    .filter((upload) => {
      if (upload.status === "done" || upload.status === "cancelled") return false;
      if (!isTerminal(upload.status)) return true;
      const created = Date.parse(String(upload.created_at));
      return Number.isNaN(created) || created >= since;
    })
    .slice(0, limit);
}

/** Whether anything listed is still moving, so the page asks the server again. */
export function anyRunning(uploads: readonly Upload[]): boolean {
  return uploads.some((upload) => !isTerminal(upload.status));
}

/** Every key an upload can be worded with, for the test that requires both languages. */
export const UPLOAD_STATUS_KEYS: readonly string[] = [
  "addOriginal.upload.watch.heading",
  "addOriginal.upload.watch.leave",
  "addOriginal.upload.watch.hashing",
  "addOriginal.upload.watch.frames",
  "addOriginal.upload.watch.framesWaiting",
  "addOriginal.upload.watch.added",
  "addOriginal.upload.watch.failed",
  "addOriginal.upload.watch.fallback",
  "addOriginal.upload.watch.deleted",
  "addOriginal.upload.watch.unreachable",
  "addOriginal.upload.open.heading",
  ...UPLOAD_STATES.map((state) => `addOriginal.upload.state.${state}`),
  ...UPLOAD_FAILURE_REASONS.map((reason) => `addOriginal.upload.reasons.${reason}`),
];
