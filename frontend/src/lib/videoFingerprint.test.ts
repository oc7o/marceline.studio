import { describe, expect, it } from "vitest";

import { Sha256, sha256File, sha256Hex } from "./sha256";
import {
  BROWSER_PIPELINE,
  DEFAULT_FPS,
  FINGERPRINT_FORMAT,
  FINGERPRINT_VERSION,
  FRAME_MAX_WIDTH,
  ORIENTATIONS,
  buildFingerprint,
  frameCount,
  frameDocuments,
  fingerprintProblems,
  rgbaToRgb,
  sampleTimestamps,
  targetSize,
} from "./videoFingerprint";

const HASH = "ab".repeat(32);

function documentWith(frames = frameDocuments(0, DEFAULT_FPS, 90, Array(ORIENTATIONS).fill(HASH))) {
  return buildFingerprint({
    sha256: "cd".repeat(32),
    fileSize: 1024,
    durationS: 3,
    fps: DEFAULT_FPS,
    title: "An original",
    creators: ["Sasha Meyer", "Carla Bremer"],
    frames,
    client: { tool: "marceline-browser 0.1.0" },
  });
}

describe("sampleTimestamps", () => {
  it("is the server's grid: k / fps while below the duration", () => {
    expect(sampleTimestamps(3, 1)).toEqual([0, 1, 2]);
    expect(sampleTimestamps(2, 2)).toEqual([0, 0.5, 1, 1.5]);
    // A duration that lands exactly on a grid point does not add that point.
    expect(sampleTimestamps(1, 1)).toEqual([0]);
  });

  it("returns nothing for an unusable duration and rejects a bad fps", () => {
    expect(sampleTimestamps(0, 1)).toEqual([]);
    expect(sampleTimestamps(Number.NaN, 1)).toEqual([]);
    expect(() => sampleTimestamps(3, 0)).toThrow(RangeError);
    expect(() => sampleTimestamps(3, Number.POSITIVE_INFINITY)).toThrow(RangeError);
  });
});

describe("targetSize", () => {
  it("mirrors scale='min(512,iw)':-2", () => {
    expect(targetSize(1920, 1080)).toEqual({ width: FRAME_MAX_WIDTH, height: 288 });
    expect(targetSize(1280, 720)).toEqual({ width: FRAME_MAX_WIDTH, height: 288 });
    // Never upscales a frame that is already narrow enough.
    expect(targetSize(320, 240)).toEqual({ width: 320, height: 240 });
    expect(targetSize(512, 384)).toEqual({ width: 512, height: 384 });
  });

  it("keeps the height even", () => {
    const { height } = targetSize(1000, 563);
    expect(height % 2).toBe(0);
  });

  it("rounds the height to the nearest even number like ffmpeg", () => {
    // The reviewer's examples: 512 * 723 / 1280 = 289.2 -> 290, not the floor 288.
    expect(targetSize(1280, 723)).toEqual({ width: 512, height: 290 });
    // Narrower than 512: no upscale, but ffmpeg still evens an odd height (271 -> 272).
    expect(targetSize(480, 271)).toEqual({ width: 480, height: 272 });
    expect(targetSize(320, 241)).toEqual({ width: 320, height: 242 });
    // 512 * 1081 / 1920 = 288.28 -> 144.14 halves -> 288; 854x480 -> 287.8 -> 288.
    expect(targetSize(1920, 1081)).toEqual({ width: 512, height: 288 });
    expect(targetSize(854, 480)).toEqual({ width: 512, height: 288 });
    // Exactly half way rounds up, as av_rescale does: 512 * 1 / 2 -> 128 halves.
    expect(targetSize(1024, 510)).toEqual({ width: 512, height: 256 });
  });

  it("rejects a frame with no pixels", () => {
    expect(() => targetSize(0, 100)).toThrow(RangeError);
    expect(() => targetSize(100, -1)).toThrow(RangeError);
  });
});

describe("rgbaToRgb", () => {
  it("drops the alpha channel", () => {
    const rgba = new Uint8ClampedArray([1, 2, 3, 255, 4, 5, 6, 128]);
    expect([...rgbaToRgb(rgba, 2)]).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it("refuses a short buffer", () => {
    expect(() => rgbaToRgb(new Uint8ClampedArray(4), 2)).toThrow(RangeError);
  });
});

describe("frameDocuments", () => {
  it("numbers the orientations and derives the timestamp from the index", () => {
    const rows = frameDocuments(4, 2, 77, Array(ORIENTATIONS).fill(HASH));
    expect(rows).toHaveLength(ORIENTATIONS);
    expect(rows.map((row) => row.dihedral)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(rows[0]?.timestamp_s).toBe(2);
    expect(rows.every((row) => row.quality === 77)).toBe(true);
  });

  it("insists on a complete orientation set", () => {
    expect(() => frameDocuments(0, 1, 90, [HASH])).toThrow(RangeError);
  });
});

describe("buildFingerprint", () => {
  it("produces the v3 video document the server validates, like the CLI's", () => {
    const document = documentWith();
    expect(document.format).toBe(FINGERPRINT_FORMAT);
    expect(document.version).toBe(3);
    expect(FINGERPRINT_VERSION).toBe(3);
    expect(document.kind).toBe("video");
    // The browser recipe is NOT the CLI's: its hashes must stay distinguishable.
    expect(document.pipeline).toBe(BROWSER_PIPELINE);
    expect(document.pipeline).not.toBe("ffmpeg-rgb512-pdq-v0");
    expect(document.dihedral).toBe(true);
    // The primary creator (rights holder) stays first.
    expect(document.creator).toEqual(["Sasha Meyer", "Carla Bremer"]);
    expect(frameCount(document)).toBe(1);
  });

  it("carries nothing but hashes and metadata", () => {
    const keys = Object.keys(documentWith()).sort();
    expect(keys).toEqual([
      "client",
      "creator",
      "dihedral",
      "duration_s",
      "file_size",
      "format",
      "fps",
      "frames",
      "kind",
      "pipeline",
      "sha256",
      "title",
      "version",
    ]);
  });

  it("refuses an empty title and an implausible cast", () => {
    expect(() => buildFingerprint({ ...documentWith(), title: "   " } as never)).toThrow();
    expect(() =>
      buildFingerprint({
        sha256: "cd".repeat(32),
        fileSize: 1,
        durationS: 1,
        fps: 1,
        title: "x",
        creators: Array.from({ length: 21 }, (_, i) => `Creator ${i}`),
        frames: [],
        client: {},
      }),
    ).toThrow(RangeError);
  });
});

describe("fingerprintProblems", () => {
  it("accepts a well-formed document", () => {
    expect(fingerprintProblems(documentWith())).toEqual([]);
  });

  it("reports an empty run", () => {
    expect(fingerprintProblems(documentWith([]))).toContain("no frame reached the quality threshold");
  });

  it("catches an incomplete orientation set", () => {
    const frames = frameDocuments(0, DEFAULT_FPS, 90, Array(ORIENTATIONS).fill(HASH)).slice(0, 7);
    expect(fingerprintProblems(documentWith(frames))).toContain(
      `frame 0 does not have all ${ORIENTATIONS} orientations`,
    );
  });

  it("catches a low quality, a bad hash and mixed qualities", () => {
    const frames = frameDocuments(0, DEFAULT_FPS, 90, Array(ORIENTATIONS).fill(HASH));
    const first = frames[0];
    const second = frames[1];
    if (!first || !second) throw new Error("fixture");
    first.quality = 10;
    second.pdq = "zz".repeat(32);
    const problems = fingerprintProblems(documentWith(frames));
    expect(problems).toContain("frame 0: quality 10 outside 50..100");
    expect(problems).toContain("frame 0: pdq is not 64 lowercase hex characters");
    expect(problems).toContain("frame 0 has mixed qualities");
  });

  it("catches a timestamp that is not frame_index / fps", () => {
    const frames = frameDocuments(2, DEFAULT_FPS, 90, Array(ORIENTATIONS).fill(HASH));
    const first = frames[0];
    if (!first) throw new Error("fixture");
    first.timestamp_s = 99;
    expect(fingerprintProblems(documentWith(frames))).toContain(
      "frame 2: timestamp_s is not frame_index / fps",
    );
  });
});

describe("sha256", () => {
  // NIST FIPS 180-4 vectors: the server compares this digest against the file it never sees.
  it("matches the published test vectors", () => {
    expect(sha256Hex(new Uint8Array(0))).toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
    expect(sha256Hex(new TextEncoder().encode("abc"))).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
    expect(
      sha256Hex(
        new TextEncoder().encode(
          "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
        ),
      ),
    ).toBe("248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
  });

  it("is chunk-boundary independent", () => {
    const data = new Uint8Array(1000).map((_, i) => i % 251);
    const once = sha256Hex(data);
    for (const size of [1, 63, 64, 65, 127, 128, 999]) {
      const hasher = new Sha256();
      for (let at = 0; at < data.length; at += size) {
        hasher.update(data.subarray(at, at + size));
      }
      expect(hasher.digest()).toBe(once);
    }
  });

  it("streams a blob and reports progress", async () => {
    const data = new Uint8Array(300).fill(7);
    const seen: number[] = [];
    const digest = await sha256File(new Blob([data]), (bytes) => seen.push(bytes));
    expect(digest).toBe(sha256Hex(data));
    expect(seen.at(-1)).toBe(300);
  });

  it("refuses to be reused after digest", () => {
    const hasher = new Sha256();
    hasher.digest();
    expect(() => hasher.digest()).toThrow();
    expect(() => hasher.update(new Uint8Array(1))).toThrow();
  });
});
