/**
 * Fingerprint an original in the browser: digest, decode, hash, build the upload document.
 *
 * **The file never leaves the machine.** Nothing here reads the file for any purpose but hashing,
 * and the only thing that can be uploaded is the `FingerprintDocument` this returns: sha256, size,
 * duration, sampling rate, title, creators and PDQ hashes. No file, no frame, no thumbnail.
 *
 * An original is a video or a **photo collection** (`fingerprintPhotos`, issue #116, ADR 0053).
 * The collection path needs no page at all — `createImageBitmap` and `OffscreenCanvas` work in the
 * Worker — so it hands over one file at a time and gets back nothing but hashes.
 *
 * The page decodes (an `HTMLVideoElement` exists only there); the Worker (`fingerprintWorker.ts`)
 * digests the file and hashes the frames, **at the same time**, so the UI keeps responding and a
 * large original costs one pass of reading rather than two in a row. The frame source and the
 * hasher are injectable, which is how the tests drive this without a DOM.
 *
 * Load this module lazily (`import("…/fingerprintClient")`): it pulls in the Worker and, through
 * it, the PDQ WASM, and only the pages that fingerprint need either.
 */

import {
  DEFAULT_FPS,
  type FingerprintDocument,
  type FingerprintFrameDocument,
  MIN_QUALITY,
  buildFingerprint,
  fingerprintProblems,
  frameDocuments,
  rgbaToRgb,
  sampleTimestamps,
  stem,
  targetSize,
} from "./videoFingerprint";
import {
  type HashedImage,
  MAX_UPLOAD_IMAGES,
  type PhotoFingerprintDocument,
  buildPhotoFingerprint,
  collectionTitle,
  photoFingerprintProblems,
} from "./photoFingerprint";
import { sha256File } from "./sha256";
import type {
  HashedMessage,
  ImageFailureReason,
  ImageHashedMessage,
  WorkerRequest,
  WorkerResponse,
} from "./fingerprintWorker";

/** The browser refused to decode this file at all - the `codec_unsupported` counter. */
export class CodecUnsupportedError extends Error {
  readonly container: string;
  readonly codec: string;

  constructor(message: string, container = "", codec = "") {
    super(message);
    this.name = "CodecUnsupportedError";
    this.container = container;
    this.codec = codec;
  }
}

/**
 * Why a local run failed, as a code a UI can put into words. `cancelled` is the user's own choice
 * and is never counted as a failure.
 */
export type FailureReason =
  | "cancelled"
  | "open_failed"
  | "no_duration"
  | "too_long"
  // A photo set with more images than one upload may carry (ADR 0053). Its own reason, because
  // the advice differs from a video's: send fewer photographs, not a shorter file.
  | "too_many_images"
  | "decode_failed"
  | "no_canvas"
  | "no_usable_frames"
  | "worker_failed";

/** Any other local failure - the `client_failed` counter. */
export class ClientFailedError extends Error {
  readonly reason: FailureReason;

  constructor(message: string, reason: FailureReason = "worker_failed") {
    super(message);
    this.name = "ClientFailedError";
    this.reason = reason;
  }
}

export interface DecodedFrame {
  /** Packed RGB24, already scaled to the pipeline's frame size. */
  rgb: Uint8Array;
  width: number;
  height: number;
}

export interface FrameSource {
  readonly durationS: number;
  /** The frame shown at `timeS`; the decoder picks the last frame whose pts is <= t. */
  frameAt(timeS: number): Promise<DecodedFrame>;
  close(): void;
}

export interface FrameHasher {
  hash(frame: DecodedFrame, frameIndex: number): Promise<{ quality: number; hashes: string[] }>;
  /** SHA-256 of the whole file off the main thread; without it the page digests the file itself. */
  digest?(file: Blob, onProgress: (bytes: number) => void): Promise<string>;
  /**
   * One image of a photo collection, end to end in the Worker: digest, decode, hash (ADR 0053).
   * Rejects with an `ImageFailedError` for a picture this browser cannot read; the set goes on.
   */
  hashImage?(file: Blob, index: number): Promise<HashedImage>;
  close(): void;
}

export type ProgressStage = "digest" | "hash" | "done";

export interface FingerprintProgress {
  /** The part that just advanced. Digest and hashing run at the same time. */
  stage: ProgressStage;
  /** Bytes digested, or frame positions hashed, of that part. */
  done: number;
  total: number;
  /** Both parts at once, for a UI that shows them side by side. */
  bytesDigested: number;
  fileSize: number;
  framesHashed: number;
  /** Positions to hash; 0 until the file's duration is known. */
  framesTotal: number;
  /**
   * Milliseconds since this run started — the run's own clock, not the caller's.
   *
   * It lives here because the run is the only thing that knows when it began, and because a React
   * component may not read a clock at all (`react-hooks/purity`). It starts before the digest, so
   * time spent opening the file counts: an estimate built on it is conservative rather than eager
   * (`fingerprintProgress.estimateRemainingMs`).
   */
  elapsedMs: number;
}

/** What `fingerprintFile` needs from an `AbortSignal`; a plain `{ aborted }` object works too. */
export interface CancelSignal {
  readonly aborted: boolean;
  addEventListener?(type: "abort", listener: () => void): void;
  removeEventListener?(type: "abort", listener: () => void): void;
}

export interface FingerprintOptions {
  fps?: number;
  title?: string;
  creators?: readonly string[];
  /** Refuse, before hashing, a video with more sampled positions than this (`too_long`). */
  maxPositions?: number;
  onProgress?: (progress: FingerprintProgress) => void;
  createSource?: (file: File) => Promise<FrameSource>;
  createHasher?: () => FrameHasher;
  client?: Record<string, string>;
  signal?: CancelSignal;
}

/** Software that computed the hashes. Deliberately no host name, user name or path. */
export function clientInfo(userAgent = ""): Record<string, string> {
  return {
    tool: "marceline-browser 0.2.0",
    // A product token, not a fingerprint of the machine: it says which decoder produced the frames.
    agent: userAgent.slice(0, 200),
  };
}

function cancelledError(): ClientFailedError {
  return new ClientFailedError("Cancelled", "cancelled");
}

/** The Worker's answer for one image, as the builder consumes it. */
function hashedImage(data: ImageHashedMessage): HashedImage {
  return { sha256: data.sha256, size: data.size, quality: data.quality, hashes: data.hashes };
}

export async function fingerprintFile(
  file: File,
  options: FingerprintOptions = {},
): Promise<FingerprintDocument> {
  const fps = options.fps ?? DEFAULT_FPS;
  const signal = options.signal;
  const startedAt = Date.now();
  const state = { bytesDigested: 0, fileSize: file.size, framesHashed: 0, framesTotal: 0 };
  const report = (stage: ProgressStage, done: number, total: number) =>
    options.onProgress?.({ stage, done, total, ...state, elapsedMs: Date.now() - startedAt });
  const throwIfCancelled = () => {
    if (signal?.aborted) throw cancelledError();
  };
  throwIfCancelled();

  let source: FrameSource | null = null;
  const hasher = (options.createHasher ?? createWorkerHasher)();
  // Closing both is how a cancel interrupts a pending seek or hash; `close` must be idempotent.
  let closed = false;
  const closeAll = () => {
    if (closed) return;
    closed = true;
    source?.close();
    hasher.close();
  };
  signal?.addEventListener?.("abort", closeAll);
  // A seek the decoder never answers must not outlive a cancel: every await gives way to it.
  const guard = <T>(promise: Promise<T>): Promise<T> => {
    if (!signal?.addEventListener) return promise;
    return new Promise<T>((resolve, reject) => {
      const abort = () => reject(cancelledError());
      signal.addEventListener?.("abort", abort);
      if (signal.aborted) abort();
      promise.then(resolve, reject).finally(() => signal.removeEventListener?.("abort", abort));
    });
  };

  const onDigestProgress = (bytes: number) => {
    state.bytesDigested = bytes;
    report("digest", bytes, file.size);
  };
  const digest = hasher.digest
    ? hasher.digest(file, onDigestProgress)
    : sha256File(file, onDigestProgress);
  // Awaited below; until then a failure must not surface as an unhandled rejection.
  digest.catch(() => undefined);

  const frames: FingerprintFrameDocument[] = [];
  try {
    const opened = await guard((options.createSource ?? createVideoFrameSource)(file));
    source = opened;
    if (closed) opened.close();
    const durationS = opened.durationS;
    const times = sampleTimestamps(durationS, fps);
    if (times.length === 0) {
      throw new ClientFailedError("The file reports no usable duration", "no_duration");
    }
    if (options.maxPositions !== undefined && times.length > options.maxPositions) {
      throw new ClientFailedError(
        `The video is too long: ${times.length} positions, at most ${options.maxPositions}`,
        "too_long",
      );
    }
    state.framesTotal = times.length;
    report("hash", 0, times.length);
    for (const [frameIndex] of times.entries()) {
      throwIfCancelled();
      const frame = await guard(opened.frameAt(frameIndex / fps));
      const { quality, hashes } = await guard(hasher.hash(frame, frameIndex));
      // The server drops featureless frames the same way, so both sides store the same positions.
      if (quality >= MIN_QUALITY) {
        frames.push(...frameDocuments(frameIndex, fps, quality, hashes));
      }
      state.framesHashed = frameIndex + 1;
      report("hash", frameIndex + 1, times.length);
    }
    const sha256 = await guard(digest);
    throwIfCancelled();
    const document = buildFingerprint({
      sha256,
      fileSize: file.size,
      durationS,
      fps,
      title: options.title?.trim() || stem(file.name),
      creators: options.creators ?? [],
      frames,
      client: options.client ?? clientInfo(),
    });
    const problems = fingerprintProblems(document);
    if (problems.length > 0) {
      throw new ClientFailedError(
        `This run produced nothing the server would accept: ${problems[0]}`,
        "no_usable_frames",
      );
    }
    report("done", frames.length, frames.length);
    return document;
  } catch (error: unknown) {
    // A cancel closes the decoder and the worker, whose pending promises then fail with their own
    // messages; the user asked to stop, so that is what the caller hears.
    if (signal?.aborted) throw cancelledError();
    throw error;
  } finally {
    signal?.removeEventListener?.("abort", closeAll);
    closeAll();
  }
}

/** One image of a collection this browser could not use. Never fatal: the set goes on without it. */
export class ImageFailedError extends Error {
  readonly reason: ImageFailureReason;

  constructor(message: string, reason: ImageFailureReason) {
    super(message);
    this.name = "ImageFailedError";
    this.reason = reason;
  }
}

/** One skipped image, as the screen reports it: a position and a reason — **never a file name**. */
export interface SkippedImage {
  /** Position in the list the customer chose, so "3 of 40" can be said without naming anything. */
  index: number;
  reason: ImageFailureReason;
  /** The browser's MIME type for it (`image/heic`), which is the useful advice. May be empty. */
  type: string;
}

export interface PhotoProgress {
  /** Images read so far, failures included. */
  done: number;
  total: number;
  skipped: number;
}

export interface PhotoFingerprintResult {
  document: PhotoFingerprintDocument;
  /** What this browser could not read. The document describes only the images it did. */
  skipped: readonly SkippedImage[];
}

export interface PhotoFingerprintOptions {
  title?: string;
  creators?: readonly string[];
  /** Refuse, before hashing, a set larger than this (`too_many_images`). */
  maxImages?: number;
  onProgress?: (progress: PhotoProgress) => void;
  createHasher?: () => FrameHasher;
  client?: Record<string, string>;
  signal?: CancelSignal;
}

/**
 * Fingerprint a photo collection in the browser (issue #116, ADR 0053).
 *
 * **The photographs never leave the machine.** Each file is handed to the Worker, which digests,
 * decodes and hashes it and returns 8 × 64 hex characters plus a quality; the only thing that can
 * be uploaded is the `PhotoFingerprintDocument` this returns — no file, no picture, no thumbnail,
 * **no file name** and no EXIF. Nothing here renders anything: the customer app is incapable of
 * showing an image at all, and their own photographs are not the exception.
 *
 * One image at a time, so memory is bounded by the largest picture rather than by the set. A
 * picture this browser cannot decode (HEIC is the usual one) is counted in `skipped` and the set
 * goes on; a set where *nothing* decoded fails, so the run is counted like any other local failure.
 */
export async function fingerprintPhotos(
  files: readonly File[],
  options: PhotoFingerprintOptions = {},
): Promise<PhotoFingerprintResult> {
  const signal = options.signal;
  const throwIfCancelled = () => {
    if (signal?.aborted) throw cancelledError();
  };
  throwIfCancelled();
  if (files.length === 0) {
    throw new ClientFailedError("No photos were chosen", "no_usable_frames");
  }
  const maxImages = options.maxImages ?? MAX_UPLOAD_IMAGES;
  if (files.length > maxImages) {
    throw new ClientFailedError(
      `The set is too large: ${files.length} images, at most ${maxImages}`,
      "too_many_images",
    );
  }

  const hasher = (options.createHasher ?? createWorkerHasher)();
  const hashImage = hasher.hashImage?.bind(hasher);
  if (hashImage === undefined) {
    hasher.close();
    throw new ClientFailedError("This build cannot hash images", "worker_failed");
  }
  let closed = false;
  const closeAll = () => {
    if (closed) return;
    closed = true;
    hasher.close();
  };
  signal?.addEventListener?.("abort", closeAll);

  const images: HashedImage[] = [];
  const skipped: SkippedImage[] = [];
  try {
    for (const [index, file] of files.entries()) {
      throwIfCancelled();
      try {
        images.push(await hashImage(file, index));
      } catch (error: unknown) {
        if (signal?.aborted) throw cancelledError();
        if (!(error instanceof ImageFailedError)) throw error;
        skipped.push({ index, reason: error.reason, type: file.type });
      }
      options.onProgress?.({ done: index + 1, total: files.length, skipped: skipped.length });
    }
    throwIfCancelled();
    if (images.length === 0) {
      // Every picture was refused by the decoder: that is the `codec_unsupported` counter, the
      // same one a video the browser will not open produces (ADR 0019).
      if (skipped.every((image) => image.reason === "decode_failed")) {
        throw new CodecUnsupportedError(
          "This browser could not read any of these images. JPEG, PNG, WebP and AVIF work; " +
            "export an HEIC set as JPEG first.",
          files[0]?.type ?? "",
        );
      }
      throw new ClientFailedError("No photo could be read on this computer", "no_usable_frames");
    }
    const document = buildPhotoFingerprint({
      images,
      title: options.title?.trim() || collectionTitle(files.map((file) => file.name)),
      creators: options.creators ?? [],
      client: options.client ?? clientInfo(),
    });
    const problems = photoFingerprintProblems(document);
    if (problems.length > 0) {
      throw new ClientFailedError(
        `This run produced nothing the server would accept: ${problems[0]}`,
        "no_usable_frames",
      );
    }
    return { document, skipped };
  } catch (error: unknown) {
    if (signal?.aborted) throw cancelledError();
    throw error;
  } finally {
    signal?.removeEventListener?.("abort", closeAll);
    closeAll();
  }
}

// Lives with the other pure helpers, so a form can use it without loading this module.
export { stem } from "./videoFingerprint";

/**
 * Decode with `<video>` + canvas.
 *
 * WebCodecs would be faster, but `VideoDecoder` needs a demuxer to hand it encoded chunks, and
 * that is a dependency this client does not have yet (ADR 0019). `<video>` uses the very same
 * platform decoders, so codec support is identical; only the seek per frame is slower.
 */
export async function createVideoFrameSource(file: File): Promise<FrameSource> {
  const url = URL.createObjectURL(file);
  const video = document.createElement("video");
  video.preload = "auto";
  video.muted = true;
  // Never render the original: it is only a pixel source.
  video.style.display = "none";
  const cleanup = () => {
    video.removeAttribute("src");
    video.load();
    URL.revokeObjectURL(url);
  };
  try {
    await new Promise<void>((resolve, reject) => {
      video.addEventListener("loadedmetadata", () => resolve(), { once: true });
      video.addEventListener(
        "error",
        () => {
          const code = video.error?.code;
          reject(
            code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED || code === MediaError.MEDIA_ERR_DECODE
              ? new CodecUnsupportedError(
                  "This browser cannot decode the file. Chrome or Edge decode H.264 MP4/MOV; " +
                    "use the CLI for ProRes, HEVC in Firefox, or anything else it refuses.",
                  file.type,
                )
              : new ClientFailedError("The file could not be opened", "open_failed"),
          );
        },
        { once: true },
      );
      video.src = url;
    });
    if (!Number.isFinite(video.duration) || video.duration <= 0) {
      throw new ClientFailedError("The file reports no usable duration", "no_duration");
    }
    if (video.videoWidth === 0 || video.videoHeight === 0) {
      throw new CodecUnsupportedError("The file carries no decodable video track", file.type);
    }
    const size = targetSize(video.videoWidth, video.videoHeight);
    const canvas = document.createElement("canvas");
    canvas.width = size.width;
    canvas.height = size.height;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (context === null) throw new ClientFailedError("This browser gave us no 2D canvas", "no_canvas");
    return {
      durationS: video.duration,
      async frameAt(timeS: number): Promise<DecodedFrame> {
        await seekTo(video, timeS);
        context.drawImage(video, 0, 0, size.width, size.height);
        const pixels = context.getImageData(0, 0, size.width, size.height).data;
        return { rgb: rgbaToRgb(pixels, size.width * size.height), ...size };
      },
      close: cleanup,
    };
  } catch (error: unknown) {
    cleanup();
    throw error;
  }
}

/**
 * Longest wait for the seeked-to picture to reach the compositor. A completed seek presents its
 * frame within a frame interval; a seek that lands inside the picture already on screen presents
 * nothing at all, and that one is already the right picture — so this is a cap, not a delay.
 */
const PRESENTED_TIMEOUT_MS = 250;
/**
 * How far before the seek target a presented frame may sit and still be the one we asked for.
 * A correct frame is within one frame interval (40 ms at 25 fps); the stale one this guards
 * against is a whole sampling step away.
 */
const STALE_FRAME_S = 0.25;

/**
 * Resolve once the browser has *presented* a frame at or after `target` — never hang if it
 * presents none.
 *
 * `seeked` says the seek finished, not that the new picture is what a canvas would draw.
 * `make browser-parity` measured it on 2026-09-18: in each of two runs exactly one sampled
 * position out of sixty came back with the **previous** second's picture — 60-104 bits from the
 * CLI hash, past every threshold — and which clip it hit moved between runs, so it is a race and
 * not a property of a file. `requestVideoFrameCallback` is the only signal that a frame reached
 * the compositor, and it is armed *before* the seek: a seek that presents its frame before
 * `seeked` fires would otherwise never call back. Chrome and Edge — the browsers this client
 * supports — have it; where it is missing the behaviour is what it was.
 */
/**
 * What `seekTo` needs from a `<video>` element — a real one satisfies it, and a test can hand it
 * a fake, which is the only way to exercise the race above without a browser.
 */
export interface SeekTarget {
  readonly duration: number;
  currentTime: number;
  addEventListener(type: string, listener: () => void, options?: { once?: boolean }): void;
  requestVideoFrameCallback?: (callback: VideoFrameRequestCallback) => number;
}

function presentedFrame(
  video: SeekTarget,
  target: number,
): { frame: Promise<void>; stop: () => void } {
  const request: ((callback: VideoFrameRequestCallback) => number) | undefined =
    typeof video.requestVideoFrameCallback === "function"
      ? video.requestVideoFrameCallback.bind(video)
      : undefined;
  if (request === undefined) return { frame: Promise.resolve(), stop: () => undefined };
  let done = false;
  const frame = new Promise<void>((resolve) => {
    const onFrame: VideoFrameRequestCallback = (_now, metadata) => {
      if (done) return;
      // An older picture is still on screen: wait for the next one rather than hash it.
      if (metadata.mediaTime < target - STALE_FRAME_S) request(onFrame);
      else {
        done = true;
        resolve();
      }
    };
    request(onFrame);
  });
  // Whoever gave up waiting stops the chain, so a late callback registers nothing further.
  return {
    frame,
    stop: () => {
      done = true;
    },
  };
}

export function seekTo(video: SeekTarget, timeS: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const target = Math.min(timeS, Math.max(0, video.duration - 1e-3));
    const presented = presentedFrame(video, target);
    video.addEventListener(
      "seeked",
      () => {
        // The cap starts here, not at the seek: a slow seek is not a missing frame, and a seek
        // that landed inside the picture already on screen presents nothing to wait for.
        const capped = new Promise<void>((cap) => setTimeout(cap, PRESENTED_TIMEOUT_MS));
        void Promise.race([presented.frame, capped]).then(() => {
          presented.stop();
          resolve();
        });
      },
      { once: true },
    );
    video.addEventListener(
      "error",
      () => reject(new ClientFailedError("Decoding failed", "decode_failed")),
      { once: true },
    );
    video.currentTime = target;
  });
}

/**
 * Digest and hash in `fingerprintWorker.ts`. One hash request is in flight at a time (the caller
 * awaits each), which keeps memory flat; the digest runs beside them.
 */
export function createWorkerHasher(): FrameHasher {
  const worker = new Worker(new URL("./fingerprintWorker.ts", import.meta.url), {
    type: "module",
  });
  const pending = new Map<
    number,
    { resolve: (value: HashedMessage) => void; reject: (error: Error) => void }
  >();
  const pendingImages = new Map<
    number,
    { resolve: (value: HashedImage) => void; reject: (error: Error) => void }
  >();
  let digesting: {
    resolve: (sha256: string) => void;
    reject: (error: Error) => void;
    onProgress: (bytes: number) => void;
  } | null = null;
  const failAll = (error: Error) => {
    for (const entry of pending.values()) entry.reject(error);
    pending.clear();
    for (const entry of pendingImages.values()) entry.reject(error);
    pendingImages.clear();
    digesting?.reject(error);
    digesting = null;
  };
  worker.addEventListener("message", (event: MessageEvent<WorkerResponse>) => {
    const data = event.data;
    switch (data.type) {
      case "hashed":
        pending.get(data.frameIndex)?.resolve(data);
        pending.delete(data.frameIndex);
        break;
      case "error":
        if (data.frameIndex === null) {
          failAll(new ClientFailedError(data.message));
        } else {
          pending.get(data.frameIndex)?.reject(new ClientFailedError(data.message));
          pending.delete(data.frameIndex);
        }
        break;
      case "digest-progress":
        digesting?.onProgress(data.bytes);
        break;
      case "digested":
        digesting?.resolve(data.sha256);
        digesting = null;
        break;
      case "digest-error":
        digesting?.reject(new ClientFailedError(`Reading the file failed: ${data.message}`, "open_failed"));
        digesting = null;
        break;
      case "image-hashed":
        pendingImages.get(data.index)?.resolve(hashedImage(data));
        pendingImages.delete(data.index);
        break;
      case "image-error":
        pendingImages.get(data.index)?.reject(new ImageFailedError(data.message, data.reason));
        pendingImages.delete(data.index);
        break;
      case "ready":
        break;
    }
  });
  // A worker that cannot start (or dies) must fail the run, not leave it waiting forever.
  worker.addEventListener("error", () => failAll(new ClientFailedError("The fingerprint worker stopped")));
  return {
    hash(frame, frameIndex) {
      return new Promise((resolve, reject) => {
        pending.set(frameIndex, {
          resolve: (value) => resolve({ quality: value.quality, hashes: value.hashes }),
          reject,
        });
        const request: WorkerRequest = {
          type: "hash",
          frameIndex,
          rgb: frame.rgb.buffer as ArrayBuffer,
          width: frame.width,
          height: frame.height,
        };
        worker.postMessage(request, [request.rgb]);
      });
    },
    digest(file, onProgress) {
      return new Promise((resolve, reject) => {
        digesting?.reject(new ClientFailedError("Superseded by another digest"));
        digesting = { resolve, reject, onProgress };
        const request: WorkerRequest = { type: "digest", file };
        worker.postMessage(request);
      });
    },
    hashImage(file, index) {
      return new Promise((resolve, reject) => {
        pendingImages.set(index, { resolve, reject });
        const request: WorkerRequest = { type: "hash-image", index, file };
        worker.postMessage(request);
      });
    },
    close() {
      failAll(cancelledError());
      worker.terminate();
    },
  };
}

/** The counter an upload failure belongs to (`POST …/client-events`); null for a cancel. */
export function failureKind(error: unknown): "codec_unsupported" | "client_failed" | null {
  if (error instanceof CodecUnsupportedError) return "codec_unsupported";
  if (error instanceof ClientFailedError && error.reason === "cancelled") return null;
  return "client_failed";
}
