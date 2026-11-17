/**
 * The photo-collection upload document: its constants, its shape and the default title.
 *
 * Split from the builder next door for the same reason `videoFingerprintFormat.ts` is (issue #99):
 * the screen that *offers* fingerprinting must not download the code that *does* it, and a screen
 * naming one constant would otherwise pull the whole pipeline into its first load.
 *
 * A photo collection is **one** original of N images with no order and no duration (ADR 0020), so
 * this document carries `image_count` and no time anywhere — not even a `timestamp_s` per image.
 * Everything here is data plus one pure string function.
 */

/**
 * Decoder + hash recipe of this client for images. Must be in `matching.fingerprint.PIPELINES`.
 *
 * Not `pillow-rgb-pdq-v0`, although the recipe is the same shape — the image at its own
 * resolution, RGB, PDQ, eight orientations, no scaler and no timeline. The **decoder** is the
 * platform's, not Pillow's: `createImageBitmap` is asked for no EXIF transform and no colour
 * conversion, and both are hints an implementation may ignore. PNG should agree bit for bit; JPEG,
 * WebP and AVIF are unmeasured, and ADR 0022's rule is that an unmeasured recipe never borrows
 * another recipe's tag. `make browser-parity PARITY_ARGS="--photos"` measures the gap.
 */
export const BROWSER_PHOTO_PIPELINE = "browser-rgb-pdq-v0";

/** `matching.fingerprint.KIND_PHOTOS`: this document is a collection, not a video. */
export const FINGERPRINT_KIND_PHOTOS = "photos";

/**
 * Images one upload may carry: `marceline.app.fingerprints.MAX_IMAGES`. The client stops before
 * hashing a larger set instead of after. A set is a shoot, not an archive; the CLI's 20,000 is for
 * a local import, which has no HTTP body.
 */
export const MAX_UPLOAD_IMAGES = 5_000;

/**
 * Domain prefix of the collection digest, byte for byte as
 * `matching.fingerprint.COLLECTION_DIGEST_PREFIX` spells it (the trailing newline included). It is
 * what keeps a collection's id from ever colliding with a file's sha256.
 */
export const COLLECTION_DIGEST_PREFIX = "marceline-photo-collection-v1\n";

/**
 * Image types the server decodes (`matching.fingerprint.IMAGE_SUFFIXES`), as an `accept` list.
 *
 * HEIC/HEIF is deliberately absent from both sides: Pillow is built without it here, and Chrome
 * does not decode it either. The screen says so and offers the way out rather than letting a
 * picker hide the files.
 */
export const IMAGE_ACCEPT = "image/jpeg,image/png,image/webp,image/avif";

/**
 * Most pixels one image may have before this client refuses it, mirroring Pillow's decompression
 * bomb limit (`PIL.Image.MAX_IMAGE_PIXELS`, 89,478,485). A picture past it is a malformed file or
 * an attack on the tab's memory, not a photograph.
 */
export const MAX_IMAGE_PIXELS = 89_478_485;

// The quality floor and the orientation count are PDQ's, not a video's or a collection's: one
// definition, re-exported here so a photo module needs no import from the video one.
export { MIN_QUALITY, ORIENTATIONS } from "./videoFingerprintFormat";

/** One hashed image, in one orientation. No `timestamp_s`: a collection has no time. */
export interface PhotoFrameDocument {
  frame_index: number;
  dihedral: number;
  quality: number;
  pdq: string;
}

export interface PhotoFingerprintDocument {
  format: string;
  version: number;
  kind: string;
  pipeline: string;
  sha256: string;
  file_size: number;
  image_count: number;
  dihedral: boolean;
  title: string;
  creator: string[];
  client: Record<string, string>;
  frames: PhotoFrameDocument[];
}

/**
 * A default title for a set, from the files the customer picked — **never a path**.
 *
 * The CLI names a collection after the folder holding it; a browser is given no folder, so the
 * best available name is the common leading part of the file names (`beach-01.jpg`,
 * `beach-02.jpg` → `beach`). Nothing is sent that the customer has not seen and can overwrite:
 * this only fills the title field on the screen, exactly as `stem()` does for a video.
 */
export function collectionTitle(names: readonly string[]): string {
  const bases = names.map((name) => {
    const file = name.replace(/^.*[\\/]/, "");
    const dot = file.lastIndexOf(".");
    return dot > 0 ? file.slice(0, dot) : file;
  });
  const first = bases[0];
  if (first === undefined) return "photos";
  let shared = first.length;
  for (const base of bases.slice(1)) {
    let index = 0;
    while (index < shared && index < base.length && base[index] === first[index]) index++;
    shared = index;
  }
  // Trailing separators and digits are numbering, not a name.
  const trimmed = first.slice(0, shared).replace(/[\s._-]*\d*$/, "");
  return trimmed || first || "photos";
}
