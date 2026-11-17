/**
 * Browser side of the parity harness.
 *
 * This page deliberately contains no pipeline logic of its own: it hands the file Playwright
 * picked to `fingerprintFile` from `src/lib/fingerprintClient.ts` - the very function the
 * "Add original" panel calls - and returns the resulting document. If this page and the app
 * disagreed, the measurement would be worthless.
 *
 * Nothing here uploads anything; the harness reads the result through `page.evaluate`.
 */

import {
  createVideoFrameSource,
  createWorkerHasher,
  fingerprintFile,
  fingerprintPhotos,
  seekTo,
  type DecodedFrame,
  type FrameHasher,
  type FrameSource,
  type SkippedImage,
} from "../../src/lib/fingerprintClient";
import { BROWSER_PIPELINE, type FingerprintDocument } from "../../src/lib/videoFingerprint";
import {
  BROWSER_PHOTO_PIPELINE,
  type PhotoFingerprintDocument,
} from "../../src/lib/photoFingerprintFormat";

/** What the decoder reported, so the report can say which pixels were actually hashed. */
export interface RunMeta {
  pipeline: string;
  userAgent: string;
  /** Size of the decoded frame before the pipeline's downscale. */
  nativeWidth: number;
  nativeHeight: number;
  /** Size the canvas scaled to, i.e. the buffer PDQ saw. */
  frameWidth: number;
  frameHeight: number;
  durationS: number;
  /** `HTMLVideoElement.canPlayType` for the codecs the harness generates. */
  canPlay: Record<string, string>;
  elapsedMs: number;
}

export interface RunResult {
  ok: boolean;
  error: { name: string; message: string } | null;
  fingerprint: FingerprintDocument | null;
  meta: RunMeta;
}

const CODECS: Record<string, string> = {
  h264: 'video/mp4; codecs="avc1.42E01E"',
  hevc: 'video/mp4; codecs="hvc1.1.6.L93.B0"',
  vp8: 'video/webm; codecs="vp8"',
  vp9: 'video/webm; codecs="vp9"',
  av1: 'video/mp4; codecs="av01.0.05M.08"',
};

function canPlayReport(): Record<string, string> {
  const probe = document.createElement("video");
  const report: Record<string, string> = {};
  for (const [name, type] of Object.entries(CODECS)) {
    // "" (cannot play), "maybe" or "probably".
    report[name] = probe.canPlayType(type) || "no";
  }
  return report;
}

/** Native frame size and duration, read from a throwaway element (metadata only, never rendered). */
function probeNativeSize(file: File): Promise<{ width: number; height: number; duration: number }> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.preload = "metadata";
    video.muted = true;
    const finish = (width: number, height: number, duration: number) => {
      video.removeAttribute("src");
      video.load();
      URL.revokeObjectURL(url);
      resolve({ width, height, duration });
    };
    video.addEventListener("loadedmetadata", () => {
      finish(video.videoWidth, video.videoHeight, video.duration);
    }, { once: true });
    video.addEventListener("error", () => finish(0, 0, 0), { once: true });
    video.src = url;
  });
}

function selectedFiles(): File[] {
  const input = document.getElementById("clip");
  if (!(input instanceof HTMLInputElement)) throw new Error("the file input is missing");
  const files = [...(input.files ?? [])];
  if (files.length === 0) throw new Error("no file was selected");
  return files;
}

function selectedFile(): File {
  const input = document.getElementById("clip");
  if (!(input instanceof HTMLInputElement)) throw new Error("the file input is missing");
  const file = input.files?.[0];
  if (!file) throw new Error("no file was selected");
  return file;
}

function setStatus(text: string): void {
  const status = document.getElementById("status");
  if (status) status.textContent = text;
}

async function run(fps: number): Promise<RunResult> {
  const file = selectedFile();
  const native = await probeNativeSize(file);
  const meta: RunMeta = {
    pipeline: BROWSER_PIPELINE,
    userAgent: navigator.userAgent,
    nativeWidth: native.width,
    nativeHeight: native.height,
    frameWidth: 0,
    frameHeight: 0,
    durationS: native.duration,
    canPlay: canPlayReport(),
    elapsedMs: 0,
  };
  const started = performance.now();
  setStatus(`hashing ${file.name}`);
  try {
    const fingerprint = await fingerprintFile(file, {
      fps,
      // The app's own decode path, wrapped only to record the frame geometry it produced.
      createSource: async (input: File): Promise<FrameSource> => {
        const source = await createVideoFrameSource(input);
        return {
          durationS: source.durationS,
          async frameAt(timeS: number): Promise<DecodedFrame> {
            const frame = await source.frameAt(timeS);
            meta.frameWidth = frame.width;
            meta.frameHeight = frame.height;
            return frame;
          },
          close: () => source.close(),
        };
      },
      client: {
        tool: "marceline-parity 0.1.0",
        agent: navigator.userAgent.slice(0, 200),
      },
    });
    meta.elapsedMs = Math.round(performance.now() - started);
    meta.durationS = fingerprint.duration_s;
    setStatus(`done: ${fingerprint.frames.length} rows`);
    return { ok: true, error: null, fingerprint, meta };
  } catch (error: unknown) {
    meta.elapsedMs = Math.round(performance.now() - started);
    const name = error instanceof Error ? error.name : "Error";
    const message = error instanceof Error ? error.message : String(error);
    setStatus(`failed: ${name}: ${message}`);
    return { ok: false, error: { name, message }, fingerprint: null, meta };
  }
}

/**
 * The photo half (issue #116, ADR 0053): the images of one synthetic set through the app's own
 * `fingerprintPhotos`, which hashes each picture in the Worker. This page decodes nothing itself —
 * if it and the app disagreed, the measurement would be worthless.
 */
export interface PhotoRunResult {
  ok: boolean;
  error: { name: string; message: string } | null;
  fingerprint: PhotoFingerprintDocument | null;
  /** Pictures this browser could not read: a position and a reason, never a file name. */
  skipped: SkippedImage[];
  meta: { pipeline: string; userAgent: string; elapsedMs: number; chosen: number };
}

async function runPhotos(): Promise<PhotoRunResult> {
  const files = selectedFiles();
  const meta = {
    pipeline: BROWSER_PHOTO_PIPELINE,
    userAgent: navigator.userAgent,
    elapsedMs: 0,
    chosen: files.length,
  };
  const started = performance.now();
  setStatus(`hashing ${files.length} images`);
  try {
    const result = await fingerprintPhotos(files, {
      client: { tool: "marceline-parity 0.1.0", agent: navigator.userAgent.slice(0, 200) },
    });
    meta.elapsedMs = Math.round(performance.now() - started);
    setStatus(`done: ${result.document.frames.length} rows, ${result.skipped.length} skipped`);
    return {
      ok: true,
      error: null,
      fingerprint: result.document,
      skipped: [...result.skipped],
      meta,
    };
  } catch (error: unknown) {
    meta.elapsedMs = Math.round(performance.now() - started);
    const name = error instanceof Error ? error.name : "Error";
    const message = error instanceof Error ? error.message : String(error);
    setStatus(`failed: ${name}: ${message}`);
    return { ok: false, error: { name, message }, fingerprint: null, skipped: [], meta };
  }
}

/**
 * The speed half of the harness (`speed.mjs`, issue #29): the same `fingerprintFile` run, with a
 * stopwatch around each of its two halves and no comparison at all.
 *
 * Nothing about the recipe changes here. The wrappers are pass-throughs that call the app's own
 * `createVideoFrameSource` and `createWorkerHasher` and add a `performance.now()` on either side,
 * so the wall time this reports is the wall time the customer waits:
 *
 * - `frameMs` is everything needed to produce one 512-px RGB buffer — the seek, the decode, the
 *   `drawImage`, the `getImageData` and the RGBA→RGB repack. Canvas work at 512×288 is small, so
 *   this is mostly the decoder's number; `probeSeek` below says how much of it is the seek.
 * - `hashMs` is the Worker round-trip for the eight dihedral PDQ hashes of that buffer.
 * - `digestMs` is the streamed SHA-256 of the whole file. It runs *beside* the frame loop in the
 *   same Worker, so it overlaps `hashMs` and does not simply add to the total.
 */
export interface TimedResult {
  ok: boolean;
  error: { name: string; message: string } | null;
  pipeline: string;
  userAgent: string;
  canPlay: Record<string, string>;
  durationS: number;
  fps: number;
  nativeWidth: number;
  nativeHeight: number;
  frameWidth: number;
  frameHeight: number;
  fileSize: number;
  /** Sampled positions the run asked for, and the ones whose hashes were kept. */
  positions: number;
  keptPositions: number;
  elapsedMs: number;
  frameMs: number;
  hashMs: number;
  digestMs: number;
  /** Largest `performance.memory.usedJSHeapSize` seen while the run was in flight, in bytes. */
  peakHeapBytes: number;
}

/** `performance.memory` is Chromium-only and not in the DOM types; absent elsewhere, hence 0. */
function heapBytes(): number {
  const memory = (performance as unknown as { memory?: { usedJSHeapSize?: number } }).memory;
  return memory?.usedJSHeapSize ?? 0;
}

async function runTimed(fps: number): Promise<TimedResult> {
  const file = selectedFile();
  const native = await probeNativeSize(file);
  const timing = { frameMs: 0, hashMs: 0, digestMs: 0, frameWidth: 0, frameHeight: 0 };
  let peakHeapBytes = heapBytes();
  const sampler = window.setInterval(() => {
    peakHeapBytes = Math.max(peakHeapBytes, heapBytes());
  }, 500);
  const result: TimedResult = {
    ok: false,
    error: null,
    pipeline: BROWSER_PIPELINE,
    userAgent: navigator.userAgent,
    canPlay: canPlayReport(),
    durationS: native.duration,
    fps,
    nativeWidth: native.width,
    nativeHeight: native.height,
    frameWidth: 0,
    frameHeight: 0,
    fileSize: file.size,
    positions: 0,
    keptPositions: 0,
    elapsedMs: 0,
    frameMs: 0,
    hashMs: 0,
    digestMs: 0,
    peakHeapBytes: 0,
  };
  const started = performance.now();
  setStatus(`timing ${file.name}`);
  try {
    const fingerprint = await fingerprintFile(file, {
      fps,
      // A long clip is the point of this measurement, so the app's own cap must not stop it; that
      // limit is a policy number (MAX_UPLOAD_POSITIONS), not something being measured here.
      maxPositions: Number.MAX_SAFE_INTEGER,
      createSource: async (input: File): Promise<FrameSource> => {
        const source = await createVideoFrameSource(input);
        return {
          durationS: source.durationS,
          async frameAt(timeS: number): Promise<DecodedFrame> {
            const before = performance.now();
            const frame = await source.frameAt(timeS);
            timing.frameMs += performance.now() - before;
            timing.frameWidth = frame.width;
            timing.frameHeight = frame.height;
            return frame;
          },
          close: () => source.close(),
        };
      },
      createHasher: (): FrameHasher => {
        const hasher = createWorkerHasher();
        const digest = hasher.digest;
        return {
          async hash(frame, frameIndex) {
            const before = performance.now();
            const hashed = await hasher.hash(frame, frameIndex);
            timing.hashMs += performance.now() - before;
            return hashed;
          },
          async digest(blob, onProgress) {
            const before = performance.now();
            const digested = await (digest
              ? digest.call(hasher, blob, onProgress)
              : Promise.reject(new Error("the worker hasher has no digest")));
            timing.digestMs += performance.now() - before;
            return digested;
          },
          close: () => hasher.close(),
        };
      },
      onProgress: (progress) => {
        result.positions = progress.framesTotal;
      },
      client: { tool: "marceline-speed 0.1.0", agent: navigator.userAgent.slice(0, 200) },
    });
    result.ok = true;
    result.durationS = fingerprint.duration_s;
    result.keptPositions = new Set(fingerprint.frames.map((frame) => frame.frame_index)).size;
    setStatus(`done: ${result.keptPositions} positions`);
  } catch (error: unknown) {
    const name = error instanceof Error ? error.name : "Error";
    const message = error instanceof Error ? error.message : String(error);
    result.error = { name, message };
    setStatus(`failed: ${name}: ${message}`);
  } finally {
    window.clearInterval(sampler);
  }
  result.elapsedMs = Math.round(performance.now() - started);
  result.frameMs = Math.round(timing.frameMs);
  result.hashMs = Math.round(timing.hashMs);
  result.digestMs = Math.round(timing.digestMs);
  result.frameWidth = timing.frameWidth;
  result.frameHeight = timing.frameHeight;
  result.peakHeapBytes = Math.max(peakHeapBytes, heapBytes());
  return result;
}

/**
 * How long the client's own `seekTo` takes, on its own, over the first `positions` sampled moments.
 *
 * The one number here that does not come from a whole-pipeline run: it exists to say whether
 * `frameMs` above is the seek or the canvas. It calls the exported `seekTo` — the same function
 * `createVideoFrameSource` calls — so it measures the shipped wait, `requestVideoFrameCallback`
 * guard included, and draws nothing.
 */
async function probeSeek(
  fps: number,
  positions: number,
): Promise<{ seekMs: number; seeks: number }> {
  const file = selectedFile();
  const url = URL.createObjectURL(file);
  const video = document.createElement("video");
  video.preload = "auto";
  video.muted = true;
  video.style.display = "none";
  try {
    await new Promise<void>((resolve, reject) => {
      video.addEventListener("loadedmetadata", () => resolve(), { once: true });
      video.addEventListener("error", () => reject(new Error("the probe could not open the file")), {
        once: true,
      });
      video.src = url;
    });
    const wanted = Math.min(positions, Math.max(1, Math.floor(video.duration * fps)));
    const before = performance.now();
    for (let index = 0; index < wanted; index++) await seekTo(video, index / fps);
    return { seekMs: Math.round(performance.now() - before), seeks: wanted };
  } finally {
    video.removeAttribute("src");
    video.load();
    URL.revokeObjectURL(url);
  }
}

declare global {
  interface Window {
    marcelineParity: {
      run: (fps: number) => Promise<RunResult>;
      runPhotos: () => Promise<PhotoRunResult>;
      runTimed: (fps: number) => Promise<TimedResult>;
      probeSeek: (fps: number, positions: number) => Promise<{ seekMs: number; seeks: number }>;
      canPlay: () => Record<string, string>;
    };
  }
}

window.marcelineParity = { run, runPhotos, runTimed, probeSeek, canPlay: canPlayReport };
setStatus("ready");
