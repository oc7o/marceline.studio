/**
 * The browser fingerprinting pipeline: sampling grid, frame geometry and the upload document.
 *
 * Framework-free and side-effect-free on purpose - a Web Worker and the page both import it, and
 * everything here is unit-tested without a DOM.
 *
 * ## Why the browser has its own pipeline tag
 *
 * The server and the CLI hash `ffmpeg -vf fps=F:round=up,scale='min(512,iw)':-2` RGB frames
 * (`ffmpeg-rgb512-pdq-v1`). A browser cannot run that: it decodes with the platform decoder and
 * scales on a canvas, whose filter and YUV->RGB matrix are not ffmpeg's. Measured in real headless
 * Chromium 153 (`frontend/parity`, issues #28 and #70, numbers in docs/adr/0019 and 0022):
 *
 * - The **frame choice now agrees.** `sampleTimestamps` + a `<video>` seek lands on the last
 *   decoded picture at or before t, and since issue #70 `fps=F:round=up` picks exactly the same
 *   one. Before that the server's filter emitted the picture at about t + 0.47 s under the label
 *   t, which alone put the stored hashes a median of 100 bits apart. It is now a median of 2.
 * - The **pixels** still differ, and that is what is left. Hashing the same decoded picture agrees
 *   with the CLI recipe to a median of 2 bits, but 20 (max 30 as stored) on a 1280x720 clip, where
 *   the canvas downscale does the most work. The match threshold is 31.
 *
 * So references from the two recipes are comparable but NOT equal, and must stay distinguishable -
 * hence a browser tag of its own. Changing the grid or the frame geometry on either side is a
 * pipeline change: new tag, and every reference hashed under the old one is re-hashed or left
 * alone. `browser-rgb512-pdq-v1` (2026-09-17) sizes the canvas exactly like ffmpeg's scale filter
 * (`targetSize`); v0 floored the height and still validates on the server.
 */

import {
  BROWSER_PIPELINE,
  DEFAULT_FPS,
  FINGERPRINT_FORMAT,
  FINGERPRINT_KIND,
  FINGERPRINT_VERSION,
  FRAME_MAX_WIDTH,
  MAX_CREATORS,
  MAX_TITLE_LENGTH,
  MIN_QUALITY,
  ORIENTATIONS,
} from "./videoFingerprintFormat";
import type { FingerprintDocument, FingerprintFrameDocument } from "./videoFingerprintFormat";

// The document format itself lives next door so a screen can name a constant without pulling the
// builder and the validator in with it (issue #99); it is re-exported here, so every existing
// import of this module is unchanged.
export * from "./videoFingerprintFormat";

/**
 * Sample positions as the server's grid: t = k / fps for every k with t < duration.
 *
 * The server derives `timestamp_s` the same way (`frame_index / fps`), so a position sampled here
 * means the same moment as the CLI's, even though the decoders pick the underlying frame
 * differently ("the last decoded frame with pts <= t").
 */
export function sampleTimestamps(durationS: number, fps: number = DEFAULT_FPS): number[] {
  if (!Number.isFinite(durationS) || durationS <= 0) return [];
  if (!Number.isFinite(fps) || fps <= 0) throw new RangeError("fps must be finite and positive");
  const times: number[] = [];
  for (let index = 0; index / fps < durationS; index++) {
    times.push(index / fps);
    // A pathological duration/fps combination must not hang the tab.
    if (times.length > 1_000_000) break;
  }
  return times;
}

/**
 * Frame size the canvas scales to, exactly as ffmpeg's `scale='min(512,iw)':-2` computes it.
 *
 * ffmpeg (`ff_scale_adjust_dimensions`) sets h = av_rescale(w, ih, iw * 2) * 2: the height of
 * the scaled width, rounded to the **nearest** multiple of two (halves up). That applies to every
 * frame, so a frame narrower than 512 keeps its width but still gets an even height (480x271 ->
 * 480x272). Flooring instead gave 1280x723 -> 512x288 where ffmpeg hashes 512x290 (issue L4).
 */
export function targetSize(width: number, height: number): { width: number; height: number } {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    throw new RangeError(`frame size must be positive integers, got ${width}x${height}`);
  }
  const scaledWidth = Math.min(FRAME_MAX_WIDTH, width);
  // av_rescale(a, b, c) = floor((a * b + c / 2) / c), all integers: no float rounding involved.
  const divisor = width * 2;
  const half = Math.floor((scaledWidth * height + Math.floor(divisor / 2)) / divisor);
  return { width: scaledWidth, height: Math.max(2, half * 2) };
}

/** RGBA from a canvas to the packed RGB24 buffer `hashDihedral` expects. */
export function rgbaToRgb(rgba: Uint8ClampedArray | Uint8Array, pixels: number): Uint8Array {
  if (rgba.length < pixels * 4) {
    throw new RangeError(`expected ${pixels * 4} RGBA bytes, got ${rgba.length}`);
  }
  const rgb = new Uint8Array(pixels * 3);
  for (let i = 0; i < pixels; i++) {
    rgb[3 * i] = rgba[4 * i] as number;
    rgb[3 * i + 1] = rgba[4 * i + 1] as number;
    rgb[3 * i + 2] = rgba[4 * i + 2] as number;
  }
  return rgb;
}

/** The eight orientation rows of one sampled position, as the server stores them. */
export function frameDocuments(
  frameIndex: number,
  fps: number,
  quality: number,
  hashes: readonly string[],
): FingerprintFrameDocument[] {
  if (hashes.length !== ORIENTATIONS) {
    throw new RangeError(`expected ${ORIENTATIONS} dihedral hashes, got ${hashes.length}`);
  }
  return hashes.map((pdq, dihedral) => ({
    frame_index: frameIndex,
    timestamp_s: frameIndex / fps,
    dihedral,
    quality,
    pdq,
  }));
}

export interface BuildFingerprintInput {
  sha256: string;
  fileSize: number;
  durationS: number;
  fps: number;
  title: string;
  creators: readonly string[];
  frames: readonly FingerprintFrameDocument[];
  client: Record<string, string>;
}

/**
 * The exact JSON `matching.fingerprint.from_json` validates - no file bytes, no path, no host or
 * user name. Only this document ever leaves the machine.
 */
export function buildFingerprint(input: BuildFingerprintInput): FingerprintDocument {
  const title = input.title.trim().slice(0, MAX_TITLE_LENGTH);
  if (!title) throw new RangeError("title must not be empty");
  if (input.creators.length > MAX_CREATORS) {
    throw new RangeError(`at most ${MAX_CREATORS} creators per original`);
  }
  return {
    format: FINGERPRINT_FORMAT,
    version: FINGERPRINT_VERSION,
    kind: FINGERPRINT_KIND,
    pipeline: BROWSER_PIPELINE,
    sha256: input.sha256,
    file_size: input.fileSize,
    duration_s: input.durationS,
    fps: input.fps,
    dihedral: true,
    title,
    creator: [...input.creators],
    client: { ...input.client },
    frames: [...input.frames],
  };
}

/**
 * The same structural checks the server runs, so a bad run fails here instead of after an upload.
 * Returns the problems found; an empty list means `from_json` will accept the document.
 */
export function fingerprintProblems(document: FingerprintDocument): string[] {
  const problems: string[] = [];
  if (!/^[0-9a-f]{64}$/.test(document.sha256)) problems.push("sha256 is not 64 lowercase hex characters");
  if (!(document.file_size > 0)) problems.push("file_size must be positive");
  if (!(document.duration_s > 0)) problems.push("duration_s must be finite and positive");
  if (!(document.fps > 0)) problems.push("fps must be finite and positive");
  if (document.frames.length === 0) problems.push("no frame reached the quality threshold");
  const byIndex = new Map<number, Set<number>>();
  const quality = new Map<number, number>();
  for (const frame of document.frames) {
    if (!/^[0-9a-f]{64}$/.test(frame.pdq)) problems.push(`frame ${frame.frame_index}: pdq is not 64 lowercase hex characters`);
    if (frame.quality < MIN_QUALITY || frame.quality > 100) {
      problems.push(`frame ${frame.frame_index}: quality ${frame.quality} outside ${MIN_QUALITY}..100`);
    }
    if (Math.abs(frame.timestamp_s - frame.frame_index / document.fps) > 1e-6) {
      problems.push(`frame ${frame.frame_index}: timestamp_s is not frame_index / fps`);
    }
    const seen = byIndex.get(frame.frame_index) ?? new Set<number>();
    if (seen.has(frame.dihedral)) {
      problems.push(`frame ${frame.frame_index}: duplicate orientation ${frame.dihedral}`);
    }
    seen.add(frame.dihedral);
    byIndex.set(frame.frame_index, seen);
    const known = quality.get(frame.frame_index);
    if (known !== undefined && known !== frame.quality) {
      problems.push(`frame ${frame.frame_index} has mixed qualities`);
    }
    quality.set(frame.frame_index, frame.quality);
  }
  for (const [index, seen] of byIndex) {
    if (seen.size !== ORIENTATIONS) {
      problems.push(`frame ${index} does not have all ${ORIENTATIONS} orientations`);
    }
  }
  return [...new Set(problems)];
}

/** Distinct sampled positions with stored hashes (what the server calls `frame_count`). */
export function frameCount(document: FingerprintDocument): number {
  return new Set(document.frames.map((frame) => frame.frame_index)).size;
}

/**
 * The same document as a plain record.
 *
 * The endpoint takes the fingerprint as an opaque JSON object (the server validates it with
 * `from_json`), so the generated request type spells it with an index signature. This keeps that
 * one shape difference in a named place instead of a cast at the call site.
 */
export function asRequestBody(document: FingerprintDocument): Record<string, unknown> {
  return { ...document };
}
