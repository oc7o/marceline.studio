/**
 * Progress of a fingerprinting run as the screens show it. Kept apart from `fingerprintClient.ts`
 * so a screen can render progress without pulling the fingerprinting code into its chunk.
 */

/** The fields of `FingerprintProgress` this module reads (structural, so no import is needed). */
export interface ProgressSnapshot {
  bytesDigested: number;
  fileSize: number;
  framesHashed: number;
  framesTotal: number;
  /** Milliseconds since the run started, measured by the run itself (`FingerprintProgress`). */
  elapsedMs: number;
}

/**
 * Seeking and hashing frames takes far longer than reading the file, so the frames weigh more.
 * Measured order of magnitude, not a promise: a bar that fills at an even pace, not an ETA.
 */
export const DIGEST_WEIGHT = 0.2;

function fraction(done: number, total: number): number {
  if (!(total > 0)) return 0;
  return Math.min(1, Math.max(0, done / total));
}

/** 0..1 across both halves of a run. Unknown frame totals count as not started. */
export function overallProgress(progress: ProgressSnapshot): number {
  const digest = fraction(progress.bytesDigested, progress.fileSize);
  const frames = fraction(progress.framesHashed, progress.framesTotal);
  return DIGEST_WEIGHT * digest + (1 - DIGEST_WEIGHT) * frames;
}

/** Whole percent, only ever changing when the integer does — cheap to put into React state. */
export function percent(value: number): number {
  return Math.floor(Math.min(1, Math.max(0, value)) * 100);
}

/**
 * Positions and milliseconds before an estimate is shown at all.
 *
 * The first few positions are not representative: the media element is still buffering and the
 * Worker still instantiating the WASM hasher. A wrong first number ("2 minutes") that then grows to
 * forty is worse than no number, so nothing is claimed until the run has settled.
 */
export const ETA_MIN_POSITIONS = 8;
export const ETA_MIN_ELAPSED_MS = 3_000;

/**
 * Milliseconds still to go, or `null` while there is not enough evidence to say.
 *
 * Measured per device rather than assumed: the rate that matters is this machine's, and it varies by
 * more than an order of magnitude across the resolutions and machines in
 * `docs/research/browser-fingerprinting-speed.md`. The estimate is a simple average over the whole
 * run so far — deliberately not a sliding window, which on a file whose scenes differ in seek cost
 * jumps around far more than it informs.
 *
 * The elapsed time comes **from the run**, never from a clock the caller reads: a React component may
 * not call `Date.now()` at all (`react-hooks/purity`), and the run is the only thing that knows when
 * it began.
 *
 * **The two halves run at the same time, so what is left is the slower of them, never the sum.**
 * The digest is the part that does not get cheaper when fewer moments are sampled: a pure-JS
 * SHA-256 over a multi-gigabyte original is minutes on its own, and on a big file at a low sampling
 * rate it is what the customer is actually waiting for.
 */
export function estimateRemainingMs(progress: ProgressSnapshot): number | null {
  const elapsedMs = progress.elapsedMs;
  if (!(elapsedMs >= ETA_MIN_ELAPSED_MS)) return null;
  if (!(progress.framesTotal > 0) || progress.framesHashed < ETA_MIN_POSITIONS) return null;
  const perPosition = elapsedMs / progress.framesHashed;
  let remaining = perPosition * Math.max(0, progress.framesTotal - progress.framesHashed);
  if (progress.fileSize > 0 && progress.bytesDigested > 0) {
    const perByte = elapsedMs / progress.bytesDigested;
    remaining = Math.max(remaining, perByte * Math.max(0, progress.fileSize - progress.bytesDigested));
  }
  return remaining;
}

/**
 * The estimate as whole minutes for a screen: `0` means "less than a minute".
 *
 * Coarse on purpose above ten minutes — a number that counts down 38, 41, 37 reads as a machine
 * guessing, and rounding to five keeps it still while staying honest about the order of magnitude.
 */
export function remainingMinutes(ms: number): number {
  if (!(ms >= 60_000)) return 0;
  const minutes = Math.ceil(ms / 60_000);
  return minutes <= 10 ? minutes : Math.ceil(minutes / 5) * 5;
}
