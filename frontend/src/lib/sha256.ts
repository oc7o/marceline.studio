/**
 * Incremental SHA-256 over a local file.
 *
 * `Reference.sha256` is the digest of the whole original, and originals are routinely several
 * gigabytes. WebCrypto's `crypto.subtle.digest` is one-shot: it would need the entire file in
 * memory, so it is not usable here. This is the plain FIPS 180-4 implementation, fed chunk by
 * chunk from a `Blob.stream()`, which keeps memory flat at the chunk size.
 *
 * Framework-free: a Web Worker imports it as is. Verified against the NIST test vectors in
 * sha256.test.ts.
 */

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotr(value: number, bits: number): number {
  return (value >>> bits) | (value << (32 - bits));
}

/** A running SHA-256 state; feed it with `update` and finish with `digest`. */
export class Sha256 {
  private readonly state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  private readonly block = new Uint8Array(64);
  private readonly words = new Uint32Array(64);
  private blockLength = 0;
  private totalLength = 0;
  private finished = false;

  update(chunk: Uint8Array): void {
    if (this.finished) throw new Error("Sha256: update after digest");
    this.totalLength += chunk.length;
    let offset = 0;
    if (this.blockLength > 0) {
      const needed = Math.min(64 - this.blockLength, chunk.length);
      this.block.set(chunk.subarray(0, needed), this.blockLength);
      this.blockLength += needed;
      offset = needed;
      if (this.blockLength === 64) {
        this.compress(this.block, 0);
        this.blockLength = 0;
      }
    }
    while (offset + 64 <= chunk.length) {
      this.compress(chunk, offset);
      offset += 64;
    }
    if (offset < chunk.length) {
      this.block.set(chunk.subarray(offset), 0);
      this.blockLength = chunk.length - offset;
    }
  }

  /** Lowercase hex, the spelling `matching.fingerprint` stores and validates. */
  digest(): string {
    if (this.finished) throw new Error("Sha256: digest called twice");
    const bitLength = this.totalLength * 8;
    const tail = new Uint8Array(this.blockLength < 56 ? 64 : 128);
    tail.set(this.block.subarray(0, this.blockLength), 0);
    tail[this.blockLength] = 0x80;
    // The length is 64 bits; split it so files above 4 GiB stay exact (numbers are safe to 2^53).
    const view = new DataView(tail.buffer);
    view.setUint32(tail.length - 8, Math.floor(bitLength / 0x100000000), false);
    view.setUint32(tail.length - 4, bitLength >>> 0, false);
    for (let offset = 0; offset < tail.length; offset += 64) {
      this.compress(tail, offset);
    }
    this.finished = true;
    let hex = "";
    for (const word of this.state) {
      hex += word.toString(16).padStart(8, "0");
    }
    return hex;
  }

  private compress(data: Uint8Array, offset: number): void {
    const w = this.words;
    for (let i = 0; i < 16; i++) {
      const at = offset + i * 4;
      w[i] =
        ((data[at] as number) << 24) |
        ((data[at + 1] as number) << 16) |
        ((data[at + 2] as number) << 8) |
        (data[at + 3] as number);
    }
    for (let i = 16; i < 64; i++) {
      const a = w[i - 15] as number;
      const b = w[i - 2] as number;
      const s0 = rotr(a, 7) ^ rotr(a, 18) ^ (a >>> 3);
      const s1 = rotr(b, 17) ^ rotr(b, 19) ^ (b >>> 10);
      w[i] = ((w[i - 16] as number) + s0 + (w[i - 7] as number) + s1) >>> 0;
    }
    let [h0, h1, h2, h3, h4, h5, h6, h7] = this.state as unknown as number[];
    let a = h0 as number;
    let b = h1 as number;
    let c = h2 as number;
    let d = h3 as number;
    let e = h4 as number;
    let f = h5 as number;
    let g = h6 as number;
    let h = h7 as number;
    for (let i = 0; i < 64; i++) {
      const s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + s1 + ch + (K[i] as number) + (w[i] as number)) >>> 0;
      const s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (s0 + maj) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    h0 = a;
    h1 = b;
    h2 = c;
    h3 = d;
    h4 = e;
    h5 = f;
    h6 = g;
    h7 = h;
    this.state[0] = ((this.state[0] as number) + h0) >>> 0;
    this.state[1] = ((this.state[1] as number) + h1) >>> 0;
    this.state[2] = ((this.state[2] as number) + h2) >>> 0;
    this.state[3] = ((this.state[3] as number) + h3) >>> 0;
    this.state[4] = ((this.state[4] as number) + h4) >>> 0;
    this.state[5] = ((this.state[5] as number) + h5) >>> 0;
    this.state[6] = ((this.state[6] as number) + h6) >>> 0;
    this.state[7] = ((this.state[7] as number) + h7) >>> 0;
  }
}

export function sha256Hex(data: Uint8Array): string {
  const hasher = new Sha256();
  hasher.update(data);
  return hasher.digest();
}

/**
 * Digest a local file without loading it: the stream is read chunk by chunk and nothing is kept.
 * `onProgress` reports bytes read so far, for the per-file progress bar.
 */
export async function sha256File(
  blob: Blob,
  onProgress?: (bytesRead: number) => void,
): Promise<string> {
  const hasher = new Sha256();
  const reader = blob.stream().getReader();
  let read = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value as ArrayBuffer);
      hasher.update(chunk);
      read += chunk.length;
      onProgress?.(read);
    }
  } finally {
    reader.releaseLock();
  }
  return hasher.digest();
}
