/**
 * The upload document's format: its constants, its shape and the default title.
 *
 * Split out of `videoFingerprint.ts` (issue #99) so that the screen which *offers* fingerprinting
 * does not download the code that *does* it. The builder and the validator next door are only
 * reached after the customer presses start, together with the hasher itself — but a static import
 * of one constant pulled the whole module into the first load of `/app/originals/new`, which is
 * the customer app's heaviest route. Everything here is data plus one pure string function.
 *
 * `videoFingerprint.ts` re-exports all of it, so every existing import keeps working; import from
 * **this** module wherever the importing code must not carry the pipeline.
 */

/** Decoder + scaler + hash recipe of this client. Must be in `matching.fingerprint.PIPELINES`. */
export const BROWSER_PIPELINE = "browser-rgb512-pdq-v1";

/** File format written by `matching.fingerprint.to_json`. */
export const FINGERPRINT_FORMAT = "marceline-fingerprint";
/**
 * The version `to_json` writes, so a browser document is the CLI's document: version 2 made
 * `creator` a list (rights holder first), version 3 added the required `kind`.
 */
export const FINGERPRINT_VERSION = 3;
/** This client fingerprints videos only; photo collections are the CLI's (ADR 0020). */
export const FINGERPRINT_KIND = "video";

/**
 * Samples per second; the same default as `add_reference` and `client/fingerprint.py`. The one
 * place the app's rate is set: the 2 fps pilot (ADR 0022 amendment) changes it here, and the
 * customer endpoint accepts exactly `marceline.app.fingerprints.SUPPORTED_FPS`.
 */
export const DEFAULT_FPS = 1;
/**
 * Sampled positions a customer upload may carry: `marceline.app.fingerprints.MAX_POSITIONS`
 * (three hours at 1 fps). The client stops before hashing a longer video instead of after.
 */
export const MAX_UPLOAD_POSITIONS = 10_800;
/** Widest frame the hasher sees, mirroring the server's `scale='min(512,iw)'`. */
export const FRAME_MAX_WIDTH = 512;
/** Frames below this are too featureless to match on; the server drops them as well. */
export const MIN_QUALITY = 50;
/** Orientations `pdqhash.compute_dihedral` returns, in the order the server stores. */
export const ORIENTATIONS = 8;
/** A video lists a handful of creators; more is a malformed request. */
export const MAX_CREATORS = 20;
export const MAX_TITLE_LENGTH = 255;

export interface FingerprintFrameDocument {
  frame_index: number;
  timestamp_s: number;
  dihedral: number;
  quality: number;
  pdq: string;
}

export interface FingerprintDocument {
  format: string;
  version: number;
  kind: string;
  pipeline: string;
  sha256: string;
  file_size: number;
  duration_s: number;
  fps: number;
  dihedral: boolean;
  title: string;
  creator: string[];
  client: Record<string, string>;
  frames: FingerprintFrameDocument[];
}

/** A file name without its extension, as the CLI uses `path.stem` for the default title. */
export function stem(name: string): string {
  const base = name.replace(/^.*[\\/]/, "");
  const dot = base.lastIndexOf(".");
  return (dot > 0 ? base.slice(0, dot) : base) || "original";
}
