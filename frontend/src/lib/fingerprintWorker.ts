/**
 * Everything CPU-bound about fingerprinting, off the main thread.
 *
 * - **Digest.** The page hands over the `File` (a handle, not a copy); the worker streams it chunk
 *   by chunk into the incremental SHA-256 (`sha256.ts`), so a multi-gigabyte original never sits in
 *   memory and the digest never blocks the tab. Progress is posted at most every `DIGEST_PROGRESS_BYTES`.
 * - **PDQ.** The page decodes frames (an `HTMLVideoElement` exists only there) and transfers the
 *   packed RGB buffer here; this worker computes the eight dihedral hashes per frame in WASM.
 * - **Photo collections** (issue #116, ADR 0053). An image needs no page: this worker takes the
 *   file, digests it, decodes it with `createImageBitmap` + `OffscreenCanvas` and hashes it, one
 *   picture at a time. Nothing but the digest, the quality and eight hex hashes crosses back, and
 *   the bitmap is closed before the next file is read.
 *
 * Both run at once: the digest yields between chunks, so hash requests interleave with it.
 *
 * Nothing in here touches the network. It receives pixels and a file handle and returns
 * 64-character hex strings.
 */

import { hashDihedral, initPdq, toHex } from "./pdq";
import { MAX_IMAGE_PIXELS } from "./photoFingerprintFormat";
import { sha256File, sha256Hex } from "./sha256";
import { rgbaToRgb } from "./videoFingerprint";

/** How often digest progress is reported: often enough for a smooth bar, rarely enough to be free. */
export const DIGEST_PROGRESS_BYTES = 8 * 1024 * 1024;

export interface HashRequest {
  type: "hash";
  frameIndex: number;
  /** Packed RGB24, `width * height * 3` bytes. Transferred, not copied. */
  rgb: ArrayBuffer;
  width: number;
  height: number;
}

export interface InitRequest {
  type: "init";
}

export interface DigestRequest {
  type: "digest";
  file: Blob;
}

/**
 * One image of a photo collection (issue #116, ADR 0053): digest it, decode it and hash it, all
 * here.
 *
 * A video needs the page, because an `HTMLVideoElement` exists only there. An image does not —
 * `createImageBitmap` and `OffscreenCanvas` work in a Worker — so the whole recipe for one picture
 * happens off the main thread and nothing but hashes crosses back. The caller sends one image at a
 * time, which is what keeps memory bounded by the largest picture rather than by the set.
 */
export interface HashImageRequest {
  type: "hash-image";
  /** Position in the caller's list, so an answer names the image it belongs to. */
  index: number;
  /** The file itself — a handle, not a copy. Read once, for its digest and its pixels. */
  file: Blob;
}

export type WorkerRequest = HashRequest | InitRequest | DigestRequest | HashImageRequest;

export interface ReadyMessage {
  type: "ready";
}

export interface HashedMessage {
  type: "hashed";
  frameIndex: number;
  quality: number;
  /** Eight lowercase hex hashes, in `pdqhash.compute_dihedral` order. */
  hashes: string[];
}

export interface DigestProgressMessage {
  type: "digest-progress";
  bytes: number;
}

export interface DigestedMessage {
  type: "digested";
  sha256: string;
}

export interface WorkerErrorMessage {
  type: "error";
  /** The frame that failed; null for initialisation. */
  frameIndex: number | null;
  message: string;
}

export interface DigestErrorMessage {
  type: "digest-error";
  message: string;
}

/** One image of a collection, hashed: its own digest, its quality and its eight hashes. */
export interface ImageHashedMessage {
  type: "image-hashed";
  index: number;
  /** sha256 of the file's bytes — the same bytes that were decoded, never a second read. */
  sha256: string;
  size: number;
  quality: number;
  hashes: string[];
}

/** Why one image of a collection produced nothing. The set goes on without it. */
export type ImageFailureReason = "decode_failed" | "too_large" | "read_failed";

export interface ImageErrorMessage {
  type: "image-error";
  index: number;
  reason: ImageFailureReason;
  message: string;
}

export type WorkerResponse =
  | ReadyMessage
  | HashedMessage
  | DigestProgressMessage
  | DigestedMessage
  | WorkerErrorMessage
  | DigestErrorMessage
  | ImageHashedMessage
  | ImageErrorMessage;

/** The slice of `DedicatedWorkerGlobalScope` this file uses (the app's tsconfig has no WebWorker lib). */
interface WorkerScope {
  postMessage(message: WorkerResponse): void;
  addEventListener(type: "message", listener: (event: { data: WorkerRequest }) => void): void;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** A failure of one image, carrying the reason the caller reports to the customer. */
export class ImageDecodeError extends Error {
  readonly reason: ImageFailureReason;

  constructor(message: string, reason: ImageFailureReason) {
    super(message);
    this.name = "ImageDecodeError";
    this.reason = reason;
  }
}

/**
 * Decode one image to packed RGB at its own resolution — the recipe `matching.pdq.hash_image`
 * runs on a page picture: no scaler, no EXIF transform, no colour management.
 *
 * `imageOrientation: "none"` keeps EXIF out of it deliberately: the eight dihedral orientations
 * already answer rotation and mirroring, and applying EXIF would move the reference away from how
 * a leaking site's copy is hashed (ADR 0020). `colorSpaceConversion: "none"` asks the decoder not
 * to convert into the display profile, which Pillow does not do either. Both are hints a browser
 * may ignore, which is exactly why this recipe has a pipeline tag of its own (ADR 0053).
 */
export async function decodeImage(
  file: Blob,
): Promise<{ rgb: Uint8Array; width: number; height: number }> {
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file, {
      imageOrientation: "none",
      premultiplyAlpha: "none",
      colorSpaceConversion: "none",
    });
  } catch (error: unknown) {
    throw new ImageDecodeError(message(error), "decode_failed");
  }
  try {
    const { width, height } = bitmap;
    if (width === 0 || height === 0) {
      throw new ImageDecodeError("the file carries no picture", "decode_failed");
    }
    if (width * height > MAX_IMAGE_PIXELS) {
      throw new ImageDecodeError(`${width}x${height} is past the pixel limit`, "too_large");
    }
    const canvas = new OffscreenCanvas(width, height);
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (context === null) throw new ImageDecodeError("no 2D canvas", "decode_failed");
    context.drawImage(bitmap, 0, 0);
    const pixels = context.getImageData(0, 0, width, height).data;
    return { rgb: rgbaToRgb(pixels, width * height), width, height };
  } finally {
    // The picture is released before the next file is read: memory stays bounded by one image.
    bitmap.close();
  }
}

async function hashOneImage(
  request: HashImageRequest,
  post: (message: WorkerResponse) => void,
): Promise<void> {
  let bytes: Uint8Array;
  try {
    bytes = new Uint8Array(await request.file.arrayBuffer());
  } catch (error: unknown) {
    post({ type: "image-error", index: request.index, reason: "read_failed", message: message(error) });
    return;
  }
  // The same bytes produce the digest and the pixels, so a collection's identity can never
  // describe bytes that were not hashed (`fingerprint_photos` says the same).
  const sha256 = sha256Hex(bytes);
  try {
    const picture = await decodeImage(new Blob([bytes as BlobPart]));
    const { hashes, quality } = await hashDihedral(picture.rgb, picture.width, picture.height);
    post({
      type: "image-hashed",
      index: request.index,
      sha256,
      size: bytes.length,
      quality,
      hashes: hashes.map(toHex),
    });
  } catch (error: unknown) {
    const reason: ImageFailureReason =
      error instanceof ImageDecodeError ? error.reason : "decode_failed";
    post({ type: "image-error", index: request.index, reason, message: message(error) });
  }
}

export async function handleRequest(
  request: WorkerRequest,
  post: (message: WorkerResponse) => void,
): Promise<void> {
  if (request.type === "init") {
    try {
      await initPdq();
      post({ type: "ready" });
    } catch (error: unknown) {
      post({ type: "error", frameIndex: null, message: message(error) });
    }
    return;
  }
  if (request.type === "hash-image") {
    await hashOneImage(request, post);
    return;
  }
  if (request.type === "digest") {
    let reported = 0;
    try {
      const sha256 = await sha256File(request.file, (bytes) => {
        if (bytes - reported >= DIGEST_PROGRESS_BYTES) {
          reported = bytes;
          post({ type: "digest-progress", bytes });
        }
      });
      post({ type: "digest-progress", bytes: request.file.size });
      post({ type: "digested", sha256 });
    } catch (error: unknown) {
      post({ type: "digest-error", message: message(error) });
    }
    return;
  }
  try {
    const { hashes, quality } = await hashDihedral(
      new Uint8Array(request.rgb),
      request.width,
      request.height,
    );
    post({
      type: "hashed",
      frameIndex: request.frameIndex,
      quality,
      hashes: hashes.map(toHex),
    });
  } catch (error: unknown) {
    post({ type: "error", frameIndex: request.frameIndex, message: message(error) });
  }
}

// Guarded so the module can also be imported by a test that only wants `handleRequest`.
if (typeof self !== "undefined" && typeof (self as unknown as WorkerScope).postMessage === "function") {
  const scope = self as unknown as WorkerScope;
  scope.addEventListener("message", (event) => {
    void handleRequest(event.data, (response) => scope.postMessage(response));
  });
}
