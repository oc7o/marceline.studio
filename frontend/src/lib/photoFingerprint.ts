/**
 * The browser's photo-collection pipeline: the collection's identity, the document, the checks.
 *
 * Framework-free and side-effect-free, like `videoFingerprint.ts` — a Web Worker and the page both
 * import it, and everything here is unit-tested without a DOM.
 *
 * ## One collection, one original, one identity
 *
 * A leak reposts the *set*, so a photo collection is one `Reference` with one hash per image
 * (ADR 0020). It has no single file, so its identity is a **collection digest**:
 *
 * ```
 * sha256("marceline-photo-collection-v1\n" + sorted(image sha256 digests).join("\n"))
 * ```
 *
 * byte for byte what `matching.fingerprint.collection_sha256` computes. Order-independent, so the
 * order the customer picked the files in is irrelevant; duplicate-sensitive, so two copies of a
 * photograph are two images; domain-separated, so a collection id can never equal a file's sha256.
 *
 * `frame_index` is the image's ordinal **in sorted-digest order**. The CLI orders by sorted path,
 * and a browser has no paths — nor may it send one. That difference is harmless by design: an
 * ordinal is a position and nothing else (ADR 0020), nothing compares two originals' ordinals, and
 * the identity is order-free either way.
 *
 * An image below `MIN_QUALITY` keeps its ordinal and its digest and contributes no hashes, exactly
 * as `fingerprint_photos` leaves a gap rather than shifting the rest.
 */

import { Sha256, sha256Hex } from "./sha256";
import {
  BROWSER_PHOTO_PIPELINE,
  COLLECTION_DIGEST_PREFIX,
  FINGERPRINT_KIND_PHOTOS,
  MIN_QUALITY,
  ORIENTATIONS,
  type PhotoFingerprintDocument,
  type PhotoFrameDocument,
} from "./photoFingerprintFormat";
import {
  FINGERPRINT_FORMAT,
  FINGERPRINT_VERSION,
  MAX_CREATORS,
  MAX_TITLE_LENGTH,
} from "./videoFingerprintFormat";

// The document format lives next door so a screen can name a constant without pulling this module
// in with it (issue #99); it is re-exported here, so one import reaches everything.
export * from "./photoFingerprintFormat";

/**
 * The collection digest of these images' sha256 digests — the same bytes the server computes.
 *
 * Hex digests, 64 lowercase characters each; at least one. Sorted here rather than by the caller,
 * so the identity cannot depend on the order files arrived in.
 */
export function collectionSha256(digests: readonly string[]): string {
  if (digests.length === 0) throw new RangeError("a photo collection needs at least one image");
  for (const digest of digests) {
    if (!/^[0-9a-f]{64}$/.test(digest)) {
      throw new RangeError("image digests must be 64 lowercase hex characters");
    }
  }
  const hasher = new Sha256();
  const encoder = new TextEncoder();
  hasher.update(encoder.encode(COLLECTION_DIGEST_PREFIX));
  hasher.update(encoder.encode([...digests].sort().join("\n")));
  return hasher.digest();
}

/** The sha256 of one image's bytes, as the collection digest consumes it. */
export function imageSha256(bytes: Uint8Array): string {
  return sha256Hex(bytes);
}

/** The eight orientation rows of one image, as the server stores them. */
export function imageDocuments(
  frameIndex: number,
  quality: number,
  hashes: readonly string[],
): PhotoFrameDocument[] {
  if (hashes.length !== ORIENTATIONS) {
    throw new RangeError(`expected ${ORIENTATIONS} dihedral hashes, got ${hashes.length}`);
  }
  return hashes.map((pdq, dihedral) => ({ frame_index: frameIndex, dihedral, quality, pdq }));
}

/** One image as this client hashed it, before the set has an order. */
export interface HashedImage {
  /** sha256 of the file's bytes — the collection digest is built from these. */
  sha256: string;
  size: number;
  quality: number;
  /** Eight lowercase hex hashes, in `pdqhash.compute_dihedral` order. */
  hashes: readonly string[];
}

export interface BuildPhotoFingerprintInput {
  images: readonly HashedImage[];
  title: string;
  creators: readonly string[];
  client: Record<string, string>;
}

/**
 * The exact JSON `matching.fingerprint.from_json` validates for a photo collection — no file
 * bytes, no file name, no EXIF, no path. Only this document ever leaves the machine.
 *
 * Images are ordered by their digest here, which is what fixes the ordinals; the caller may hand
 * them over in any order.
 */
export function buildPhotoFingerprint(
  input: BuildPhotoFingerprintInput,
): PhotoFingerprintDocument {
  const title = input.title.trim().slice(0, MAX_TITLE_LENGTH);
  if (!title) throw new RangeError("title must not be empty");
  if (input.creators.length > MAX_CREATORS) {
    throw new RangeError(`at most ${MAX_CREATORS} creators per original`);
  }
  if (input.images.length === 0) throw new RangeError("a photo collection needs at least one image");
  const ordered = [...input.images].sort((left, right) => (left.sha256 < right.sha256 ? -1 : 1));
  const frames: PhotoFrameDocument[] = [];
  ordered.forEach((image, index) => {
    // Below the threshold the position keeps its ordinal and contributes nothing, so the images
    // after it are not shifted — the server's own behaviour.
    if (image.quality >= MIN_QUALITY) {
      frames.push(...imageDocuments(index, image.quality, image.hashes));
    }
  });
  return {
    format: FINGERPRINT_FORMAT,
    version: FINGERPRINT_VERSION,
    kind: FINGERPRINT_KIND_PHOTOS,
    pipeline: BROWSER_PHOTO_PIPELINE,
    sha256: collectionSha256(ordered.map((image) => image.sha256)),
    file_size: ordered.reduce((total, image) => total + image.size, 0),
    image_count: ordered.length,
    dihedral: true,
    title,
    creator: [...input.creators],
    client: { ...input.client },
    frames,
  };
}

/**
 * The same structural checks the server runs, so a bad run fails here instead of after an upload.
 * Returns the problems found; an empty list means `from_json` will accept the document.
 */
export function photoFingerprintProblems(document: PhotoFingerprintDocument): string[] {
  const problems: string[] = [];
  if (!/^[0-9a-f]{64}$/.test(document.sha256)) {
    problems.push("sha256 is not 64 lowercase hex characters");
  }
  if (!(document.file_size > 0)) problems.push("file_size must be positive");
  if (!(document.image_count > 0)) problems.push("image_count must be positive");
  if (document.frames.length === 0) problems.push("no image reached the quality threshold");
  const byIndex = new Map<number, Set<number>>();
  const quality = new Map<number, number>();
  for (const frame of document.frames) {
    if (!/^[0-9a-f]{64}$/.test(frame.pdq)) {
      problems.push(`image ${frame.frame_index}: pdq is not 64 lowercase hex characters`);
    }
    if (frame.quality < MIN_QUALITY || frame.quality > 100) {
      problems.push(`image ${frame.frame_index}: quality ${frame.quality} outside ${MIN_QUALITY}..100`);
    }
    if (frame.frame_index < 0 || frame.frame_index >= document.image_count) {
      problems.push(`image ${frame.frame_index}: ordinal outside the collection`);
    }
    const seen = byIndex.get(frame.frame_index) ?? new Set<number>();
    if (seen.has(frame.dihedral)) {
      problems.push(`image ${frame.frame_index}: duplicate orientation ${frame.dihedral}`);
    }
    seen.add(frame.dihedral);
    byIndex.set(frame.frame_index, seen);
    const known = quality.get(frame.frame_index);
    if (known !== undefined && known !== frame.quality) {
      problems.push(`image ${frame.frame_index} has mixed qualities`);
    }
    quality.set(frame.frame_index, frame.quality);
  }
  for (const [index, seen] of byIndex) {
    if (seen.size !== ORIENTATIONS) {
      problems.push(`image ${index} does not have all ${ORIENTATIONS} orientations`);
    }
  }
  return [...new Set(problems)];
}

/** Distinct images with stored hashes (what the server calls `frame_count`). */
export function imageCount(document: PhotoFingerprintDocument): number {
  return new Set(document.frames.map((frame) => frame.frame_index)).size;
}

/**
 * The same document as a plain record — the endpoint takes the fingerprint as an opaque JSON
 * object and the server validates it, so this keeps that one shape difference in a named place.
 */
export function asPhotoRequestBody(document: PhotoFingerprintDocument): Record<string, unknown> {
  return { ...document };
}
