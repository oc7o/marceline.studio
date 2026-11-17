// PDQ perceptual hashing in the browser: a thin typed wrapper around Meta's C++ hasher
// compiled to WebAssembly (frontend/wasm/pdq, output in ../wasm/pdq-wasm.js).
//
// The server hashes reference frames with the `pdqhash` Python binding
// (backend/matching/fingerprint.py). This wrapper is proven to return the identical 32-byte
// hashes and quality for the identical pixel buffer - see pdq.test.ts, which replays the
// fixtures in backend/matching/tests/fixtures/pdq/.
//
// Deliberately framework-free (no React, no DOM APIs) so a Web Worker can import it.

import createPdqModule, { type PdqWasmModule } from "../wasm/pdq-wasm.js";

/** Bytes in one PDQ hash. */
export const PDQ_HASH_BYTES = 32;
/** Number of dihedral variants the hasher returns. */
export const PDQ_ORIENTATIONS = 8;
/** Below this the frame is too featureless to match on (Meta's guidance; MIN_QUALITY server-side). */
export const PDQ_MIN_QUALITY = 50;

/**
 * Index into `hashes`. Same order as `pdqhash.compute_dihedral` and as the server's
 * `ReferenceFrameHash.dihedral` column.
 */
export const DIHEDRAL = {
  original: 0,
  rotate90: 1,
  rotate180: 2,
  rotate270: 3,
  flipX: 4,
  flipY: 5,
  flipPlus1: 6,
  flipMinus1: 7,
} as const;

export type DihedralName = keyof typeof DIHEDRAL;

export interface DihedralHashes {
  /** Eight 32-byte hashes, indexed by `DIHEDRAL`. */
  hashes: Uint8Array[];
  /** PDQ quality metric, 0..100. */
  quality: number;
}

let modulePromise: Promise<PdqWasmModule> | null = null;

/**
 * Load and instantiate the WASM module once. Safe to call concurrently; callers that want to pay
 * the cost up front (a Worker starting up) can await it before the first frame arrives.
 *
 * Lazy on purpose: the module is a few hundred kB and nothing but the fingerprinting page needs
 * it, and a top-level await would drag it into the app's main chunk.
 */
export async function initPdq(): Promise<void> {
  await loadModule();
}

function loadModule(): Promise<PdqWasmModule> {
  if (modulePromise === null) {
    modulePromise = createPdqModule().catch((error: unknown) => {
      // Let the next call retry instead of caching a rejected promise forever.
      modulePromise = null;
      throw error;
    });
  }
  return modulePromise;
}

function expectedLength(kind: "rgb" | "luma", width: number, height: number): number {
  return width * height * (kind === "rgb" ? 3 : 1);
}

function checkInput(
  kind: "rgb" | "luma",
  pixels: Uint8Array,
  width: number,
  height: number,
): void {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    throw new RangeError(`width and height must be positive integers, got ${width}x${height}`);
  }
  const expected = expectedLength(kind, width, height);
  if (pixels.length !== expected) {
    throw new RangeError(
      `expected ${expected} bytes for ${width}x${height} ${kind}, got ${pixels.length}`,
    );
  }
}

function hashWith(
  module: PdqWasmModule,
  kind: "rgb" | "luma",
  pixels: Uint8Array,
  width: number,
  height: number,
): DihedralHashes {
  const outBytes = PDQ_ORIENTATIONS * PDQ_HASH_BYTES;
  const inputPtr = module._malloc(pixels.length);
  const outputPtr = module._malloc(outBytes);
  if (inputPtr === 0 || outputPtr === 0) {
    if (inputPtr !== 0) module._free(inputPtr);
    if (outputPtr !== 0) module._free(outputPtr);
    throw new Error("PDQ: out of WebAssembly memory");
  }
  try {
    // HEAPU8 is re-read after every allocation: growing the heap detaches earlier views.
    module.HEAPU8.set(pixels, inputPtr);
    const quality =
      kind === "rgb"
        ? module._marceline_pdq_dihedral_rgb(inputPtr, height, width, outputPtr)
        : module._marceline_pdq_dihedral_luma(inputPtr, height, width, outputPtr);
    if (quality < 0) {
      throw new RangeError(`PDQ rejected a ${width}x${height} frame`);
    }
    const heap = module.HEAPU8;
    const hashes: Uint8Array[] = [];
    for (let i = 0; i < PDQ_ORIENTATIONS; i++) {
      const start = outputPtr + i * PDQ_HASH_BYTES;
      // slice() copies out of the heap: the buffer is freed below.
      hashes.push(heap.slice(start, start + PDQ_HASH_BYTES));
    }
    return { hashes, quality };
  } finally {
    module._free(inputPtr);
    module._free(outputPtr);
  }
}

/**
 * Hash one packed RGB24 frame (`width * height * 3` bytes, row-major) into all eight dihedral
 * variants. Matches `pdqhash.compute_dihedral` on the server bit for bit, including the
 * double-precision luma conversion that binding uses.
 */
export async function hashDihedral(
  rgb: Uint8Array,
  width: number,
  height: number,
): Promise<DihedralHashes> {
  checkInput("rgb", rgb, width, height);
  const module = await loadModule();
  return hashWith(module, "rgb", rgb, width, height);
}

/**
 * Hash one 8-bit luma plane (`width * height` bytes, row-major), i.e. R=G=B=Y. This is the
 * canonical-pipeline path from docs/research/fingerprinting-client.md, where the browser hashes
 * the decoder's Y plane instead of converted RGB. It is NOT the recipe the current server
 * references use, so hashes from here are only comparable with other luma-path hashes.
 */
export async function hashDihedralLuma(
  luma: Uint8Array,
  width: number,
  height: number,
): Promise<DihedralHashes> {
  checkInput("luma", luma, width, height);
  const module = await loadModule();
  return hashWith(module, "luma", luma, width, height);
}

/** Lowercase hex, the canonical ThreatExchange PDQ spelling the backend stores and prints. */
export function toHex(hash: Uint8Array): string {
  if (hash.length !== PDQ_HASH_BYTES) {
    throw new RangeError(`a PDQ hash is ${PDQ_HASH_BYTES} bytes, got ${hash.length}`);
  }
  let hex = "";
  for (const byte of hash) {
    hex += byte.toString(16).padStart(2, "0");
  }
  return hex;
}

/** Parse the 64-character hex spelling back into bytes. */
export function fromHex(hex: string): Uint8Array {
  if (!/^[0-9a-f]{64}$/.test(hex)) {
    throw new RangeError(`expected ${PDQ_HASH_BYTES * 2} lowercase hex characters`);
  }
  const bytes = new Uint8Array(PDQ_HASH_BYTES);
  for (let i = 0; i < PDQ_HASH_BYTES; i++) {
    bytes[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

/** Number of differing bits between two hashes (the server's match threshold is 31). */
export function hammingDistance(first: Uint8Array, second: Uint8Array): number {
  if (first.length !== PDQ_HASH_BYTES || second.length !== PDQ_HASH_BYTES) {
    throw new RangeError(`a PDQ hash is ${PDQ_HASH_BYTES} bytes`);
  }
  let distance = 0;
  for (let i = 0; i < PDQ_HASH_BYTES; i++) {
    // Both indices are in range: the length check above guarantees it.
    let xor = (first[i] as number) ^ (second[i] as number);
    while (xor !== 0) {
      distance += xor & 1;
      xor >>>= 1;
    }
  }
  return distance;
}
