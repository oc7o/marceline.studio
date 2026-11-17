import { describe, expect, it } from "vitest";

import { DIGEST_PROGRESS_BYTES, type WorkerResponse, handleRequest } from "./fingerprintWorker";
import {
  ETA_MIN_POSITIONS,
  type ProgressSnapshot,
  estimateRemainingMs,
  overallProgress,
  percent,
  remainingMinutes,
} from "./fingerprintProgress";

/** Deterministic bytes, generated here: no media file in the repository. */
function syntheticBytes(length: number): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(length);
  let state = 0x2545f491;
  for (let i = 0; i < length; i++) {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    bytes[i] = state >>> 24;
  }
  return bytes;
}

/** A synthetic RGB frame with structure (a diagonal gradient and a bright square), not a photo. */
function syntheticFrame(width: number, height: number): Uint8Array<ArrayBuffer> {
  const rgb = new Uint8Array(width * height * 3);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const at = 3 * (y * width + x);
      const square = x > width / 4 && x < width / 2 && y > height / 3 && y < (2 * height) / 3;
      rgb[at] = square ? 250 : (x * 255) / width;
      rgb[at + 1] = square ? 240 : (y * 255) / height;
      rgb[at + 2] = ((x + y) * 127) / (width + height);
    }
  }
  return rgb;
}

async function collect(request: Parameters<typeof handleRequest>[0]): Promise<WorkerResponse[]> {
  const messages: WorkerResponse[] = [];
  await handleRequest(request, (message) => messages.push(message));
  return messages;
}

describe("worker digest", () => {
  it("streams the file into SHA-256 and matches WebCrypto's digest", async () => {
    const bytes = syntheticBytes(DIGEST_PROGRESS_BYTES * 2 + 12345);

    const messages = await collect({ type: "digest", file: new Blob([bytes]) });

    const digested = messages.find((message) => message.type === "digested");
    // WebCrypto is one-shot, fine for a test-sized buffer and an independent implementation.
    const expected = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
    expect(digested).toEqual({ type: "digested", sha256: expected });
    const progress = messages.filter((message) => message.type === "digest-progress");
    // Throttled: a handful of messages for a multi-megabyte file, ending at the full size.
    expect(progress.length).toBeGreaterThanOrEqual(2);
    expect(progress.length).toBeLessThanOrEqual(4);
    expect(progress.at(-1)).toEqual({ type: "digest-progress", bytes: bytes.length });
  });

  it("reports a read failure instead of throwing", async () => {
    const broken = { size: 10, stream: () => { throw new Error("gone"); } } as unknown as Blob;

    const messages = await collect({ type: "digest", file: broken });

    expect(messages).toEqual([{ type: "digest-error", message: "gone" }]);
  });
});

describe("worker PDQ", () => {
  it("hashes a generated frame into eight hex hashes and a quality", async () => {
    const width = 64;
    const height = 36;
    const rgb = syntheticFrame(width, height);

    const messages = await collect({ type: "hash", frameIndex: 7, rgb: rgb.buffer, width, height });

    expect(messages).toHaveLength(1);
    const [message] = messages;
    if (message?.type !== "hashed") throw new Error(`unexpected ${JSON.stringify(message)}`);
    expect(message.frameIndex).toBe(7);
    expect(message.hashes).toHaveLength(8);
    for (const hash of message.hashes) expect(hash).toMatch(/^[0-9a-f]{64}$/);
    expect(message.quality).toBeGreaterThanOrEqual(0);
    expect(message.quality).toBeLessThanOrEqual(100);
  });

  it("answers a malformed frame with an error for that frame", async () => {
    const messages = await collect({ type: "hash", frameIndex: 3, rgb: new ArrayBuffer(5), width: 4, height: 4 });

    expect(messages).toMatchObject([{ type: "error", frameIndex: 3 }]);
  });
});

describe("overallProgress", () => {
  it("weighs the frames more than the digest and stays within 0..1", () => {
    const none = { bytesDigested: 0, fileSize: 100, framesHashed: 0, framesTotal: 0, elapsedMs: 0 };
    expect(overallProgress(none)).toBe(0);
    expect(overallProgress({ ...none, bytesDigested: 100 })).toBeCloseTo(0.2);
    expect(overallProgress({ ...none, framesHashed: 5, framesTotal: 10 })).toBeCloseTo(0.4);
    expect(overallProgress({ ...none, bytesDigested: 100, framesHashed: 10, framesTotal: 10 })).toBe(1);
    expect(overallProgress({ ...none, bytesDigested: 500 })).toBeCloseTo(0.2);
  });

  it("gives whole percent", () => {
    expect(percent(0.456)).toBe(45);
    expect(percent(2)).toBe(100);
    expect(percent(-1)).toBe(0);
  });
});

describe("estimateRemainingMs", () => {
  const snapshot = (over: Partial<ProgressSnapshot> = {}): ProgressSnapshot => ({
    bytesDigested: 0,
    fileSize: 0,
    framesHashed: 0,
    framesTotal: 0,
    elapsedMs: 0,
    ...over,
  });

  it("says nothing until the run has settled", () => {
    // Too early, too few positions, and no total at all: each one is "no number yet", never a guess.
    expect(estimateRemainingMs(snapshot({ framesHashed: 100, framesTotal: 600, elapsedMs: 1_000 }))).toBeNull();
    expect(
      estimateRemainingMs(
        snapshot({ framesHashed: ETA_MIN_POSITIONS - 1, framesTotal: 600, elapsedMs: 60_000 }),
      ),
    ).toBeNull();
    expect(estimateRemainingMs(snapshot({ framesHashed: 100, framesTotal: 0, elapsedMs: 60_000 }))).toBeNull();
  });

  it("extrapolates the positions already hashed", () => {
    // 60 of 600 positions in 30 s → 500 ms each → 540 left → 270 s.
    expect(
      estimateRemainingMs(snapshot({ framesHashed: 60, framesTotal: 600, elapsedMs: 30_000 })),
    ).toBe(270_000);
  });

  it("takes the slower of the two halves, because they run at the same time", () => {
    // Frames: 300 of 600 in 30 s → 30 s left. Digest: a tenth of the file in 30 s → 270 s left.
    // A big file at a low sampling rate waits for the digest, and the estimate must say so.
    const progress = snapshot({
      framesHashed: 300,
      framesTotal: 600,
      bytesDigested: 400_000_000,
      fileSize: 4_000_000_000,
      elapsedMs: 30_000,
    });
    expect(estimateRemainingMs(progress)).toBe(270_000);
  });

  it("never goes negative when a part is already past its total", () => {
    const progress = snapshot({
      framesHashed: 600,
      framesTotal: 600,
      bytesDigested: 100,
      fileSize: 100,
      elapsedMs: 30_000,
    });
    expect(estimateRemainingMs(progress)).toBe(0);
  });
});

describe("remainingMinutes", () => {
  it("rounds up, and coarsely above ten minutes", () => {
    expect(remainingMinutes(0)).toBe(0);
    expect(remainingMinutes(59_999)).toBe(0);
    expect(remainingMinutes(60_000)).toBe(1);
    expect(remainingMinutes(61_000)).toBe(2);
    expect(remainingMinutes(10 * 60_000)).toBe(10);
    // Above ten minutes it moves in fives, so the number stops twitching while the run goes on.
    expect(remainingMinutes(11 * 60_000)).toBe(15);
    expect(remainingMinutes(37 * 60_000)).toBe(40);
    expect(remainingMinutes(40 * 60_000)).toBe(40);
  });
});
