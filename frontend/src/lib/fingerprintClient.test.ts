import { describe, expect, it, vi } from "vitest";

import {
  ClientFailedError,
  CodecUnsupportedError,
  type DecodedFrame,
  type FingerprintProgress,
  type FrameHasher,
  type FrameSource,
  clientInfo,
  failureKind,
  fingerprintFile,
  seekTo,
  stem,
} from "./fingerprintClient";
import { sha256Hex } from "./sha256";
import { BROWSER_PIPELINE, MIN_QUALITY, ORIENTATIONS } from "./videoFingerprint";

const HASHES = Array.from({ length: ORIENTATIONS }, (_, i) =>
  i.toString(16).repeat(64).slice(0, 64),
);

function fakeSource(durationS: number, onFrame?: (timeS: number) => void): FrameSource {
  return {
    durationS,
    async frameAt(timeS: number): Promise<DecodedFrame> {
      onFrame?.(timeS);
      return { rgb: new Uint8Array(4 * 4 * 3), width: 4, height: 4 };
    },
    close: vi.fn(),
  };
}

function fakeHasher(quality: number | ((frameIndex: number) => number) = 90): FrameHasher {
  return {
    async hash(_frame, frameIndex) {
      return {
        quality: typeof quality === "function" ? quality(frameIndex) : quality,
        hashes: HASHES,
      };
    },
    close: vi.fn(),
  };
}

function videoFile(name = "My Clip.mp4", bytes = new Uint8Array([1, 2, 3])): File {
  return new File([bytes], name, { type: "video/mp4" });
}

async function run(
  file: File,
  source: FrameSource,
  hasher: FrameHasher = fakeHasher(),
  extra: Parameters<typeof fingerprintFile>[1] = {},
) {
  return fingerprintFile(file, {
    createSource: async () => source,
    createHasher: () => hasher,
    ...extra,
  });
}

describe("fingerprintFile", () => {
  it("hashes the sampling grid and builds the upload document", async () => {
    const seen: number[] = [];
    const bytes = new Uint8Array([9, 8, 7, 6]);

    const document = await run(videoFile("My Clip.mp4", bytes), fakeSource(3, (t) => seen.push(t)), fakeHasher(), {
      creators: ["Sasha Meyer", "Carla Bremer"],
    });

    // One sample per second, exactly like the CLI's default fps.
    expect(seen).toEqual([0, 1, 2]);
    expect(document.frames).toHaveLength(3 * ORIENTATIONS);
    expect(document.pipeline).toBe(BROWSER_PIPELINE);
    expect(document.duration_s).toBe(3);
    expect(document.title).toBe("My Clip");
    expect(document.creator).toEqual(["Sasha Meyer", "Carla Bremer"]);
    // The digest is of the real file, computed locally.
    expect(document.sha256).toBe(sha256Hex(bytes));
    expect(document.file_size).toBe(bytes.length);
  });

  it("uploads hashes and metadata only - never the file or a frame", async () => {
    const document = await run(videoFile(), fakeSource(2));

    const serialized = JSON.stringify(document);
    expect(serialized).not.toContain("My Clip.mp4");
    expect(Object.keys(document)).not.toContain("path");
    // Every frame row is a hex hash, not pixels.
    for (const frame of document.frames) {
      expect(frame.pdq).toMatch(/^[0-9a-f]{64}$/);
    }
  });

  it("drops frames below the quality gate, as the server does", async () => {
    const hasher = fakeHasher((index) => (index === 1 ? MIN_QUALITY - 1 : 90));

    const document = await run(videoFile(), fakeSource(3), hasher);

    expect(document.frames).toHaveLength(2 * ORIENTATIONS);
    expect([...new Set(document.frames.map((frame) => frame.frame_index))]).toEqual([0, 2]);
  });

  it("reports progress for the digest and every hashed position", async () => {
    const progress: FingerprintProgress[] = [];

    await run(videoFile(), fakeSource(2), fakeHasher(), {
      onProgress: (value) => progress.push(value),
    });

    expect(progress.some((entry) => entry.stage === "digest")).toBe(true);
    const hashing = progress.filter((entry) => entry.stage === "hash");
    expect(hashing.at(-1)).toMatchObject({ stage: "hash", done: 2, total: 2, framesHashed: 2, framesTotal: 2 });
    const last = progress.at(-1);
    expect(last?.stage).toBe("done");
    // The snapshot carries both halves, so a screen can show digest and hashing side by side.
    expect(last).toMatchObject({ bytesDigested: 3, fileSize: 3, framesHashed: 2, framesTotal: 2 });
  });

  it("always closes the decoder and the worker", async () => {
    const source = fakeSource(1);
    const hasher = fakeHasher();
    const failing: FrameHasher = {
      hash: () => Promise.reject(new Error("worker died")),
      close: hasher.close,
    };

    await expect(run(videoFile(), source, failing)).rejects.toThrow("worker died");
    expect(source.close).toHaveBeenCalled();
    expect(hasher.close).toHaveBeenCalled();
  });

  it("fails loudly when nothing usable came out", async () => {
    await expect(run(videoFile(), fakeSource(0))).rejects.toBeInstanceOf(ClientFailedError);
    await expect(run(videoFile(), fakeSource(0))).rejects.toMatchObject({ reason: "no_duration" });
    // Every frame too featureless: refuse rather than upload an empty claim.
    await expect(run(videoFile(), fakeSource(2), fakeHasher(10))).rejects.toThrow(
      /nothing the server would accept/,
    );
  });

  it("can be cancelled", async () => {
    const signal = { aborted: false };
    const source = fakeSource(5, () => {
      signal.aborted = true;
    });

    await expect(run(videoFile(), source, fakeHasher(), { signal })).rejects.toMatchObject({
      message: "Cancelled",
      reason: "cancelled",
    });
  });

  it("stops a video that is too long before hashing a single frame", async () => {
    const hasher = fakeHasher();
    const hash = vi.spyOn(hasher, "hash");

    const run1 = run(videoFile(), fakeSource(11), hasher, { maxPositions: 10 });

    await expect(run1).rejects.toMatchObject({ name: "ClientFailedError", reason: "too_long" });
    expect(hash).not.toHaveBeenCalled();
  });

  it("digests in the hasher (the worker) when it can, at the same time as hashing", async () => {
    const order: string[] = [];
    let finishDigest: (value: string) => void = () => undefined;
    const hasher: FrameHasher = {
      ...fakeHasher(),
      async hash(frame, frameIndex) {
        order.push(`hash ${frameIndex}`);
        return fakeHasher().hash(frame, frameIndex);
      },
      digest(_file, onProgress) {
        order.push("digest started");
        onProgress(3);
        return new Promise((resolve) => {
          finishDigest = resolve;
        });
      },
    };

    const pending = run(videoFile(), fakeSource(2), hasher);
    await vi.waitFor(() => expect(order).toContain("hash 1"));
    finishDigest("ef".repeat(32));
    const document = await pending;

    expect(order[0]).toBe("digest started");
    expect(document.sha256).toBe("ef".repeat(32));
  });

  it("an abort interrupts a decoder that never answers", async () => {
    const controller = new AbortController();
    const source = fakeSource(5);
    source.frameAt = () => new Promise<DecodedFrame>(() => undefined);
    const hasher = fakeHasher();

    const pending = run(videoFile(), source, hasher, { signal: controller.signal });
    await new Promise((resolve) => setTimeout(resolve, 0));
    controller.abort();

    await expect(pending).rejects.toMatchObject({ reason: "cancelled" });
    expect(source.close).toHaveBeenCalled();
    expect(hasher.close).toHaveBeenCalled();
  });

  it("honours a custom fps and title", async () => {
    const document = await run(videoFile(), fakeSource(2), fakeHasher(), {
      fps: 2,
      title: "  Named by hand  ",
    });

    expect(document.fps).toBe(2);
    expect(document.title).toBe("Named by hand");
    expect(document.frames.map((frame) => frame.timestamp_s)).toContain(0.5);
  });
});

describe("failureKind", () => {
  it("separates the two counters", () => {
    expect(failureKind(new CodecUnsupportedError("no decoder"))).toBe("codec_unsupported");
    expect(failureKind(new ClientFailedError("boom"))).toBe("client_failed");
    expect(failureKind(new Error("something else"))).toBe("client_failed");
  });

  it("does not count a cancel as a failure", () => {
    expect(failureKind(new ClientFailedError("Cancelled", "cancelled"))).toBeNull();
  });
});

describe("clientInfo", () => {
  it("names the tool and the decoder, and nothing about the machine", () => {
    const info = clientInfo("Mozilla/5.0 (X11)");
    expect(info.tool).toContain("marceline-browser");
    expect(Object.keys(info).sort()).toEqual(["agent", "tool"]);
  });
});

describe("stem", () => {
  it("drops the extension and any directory", () => {
    expect(stem("My Clip.mp4")).toBe("My Clip");
    expect(stem("folder/scene.final.mov")).toBe("scene.final");
    expect(stem("noextension")).toBe("noextension");
    expect(stem(".hidden")).toBe(".hidden");
  });
});

/**
 * The stale-frame race `make browser-parity` found on 2026-09-18 (issue #29): `seeked` fires
 * before the seeked-to picture reaches the compositor, so `drawImage` could hash the *previous*
 * second. Only `requestVideoFrameCallback` says a frame was presented, and a fake video is the
 * only way to drive that without a browser.
 */
describe("seekTo", () => {
  /** A `<video>` whose `seeked` and presented frames this test fires by hand. */
  function fakeVideo(duration = 10) {
    const listeners = new Map<string, () => void>();
    const presented: VideoFrameRequestCallback[] = [];
    return {
      duration,
      currentTime: 0,
      addEventListener(type: string, listener: () => void) {
        listeners.set(type, listener);
      },
      requestVideoFrameCallback(callback: VideoFrameRequestCallback) {
        presented.push(callback);
        return presented.length;
      },
      seeked: () => listeners.get("seeked")?.(),
      /** Present one frame with the moment it carries; the client compares it with the target. */
      present: (mediaTime: number) =>
        presented.shift()?.(0, { mediaTime } as VideoFrameCallbackMetadata),
      pending: () => presented.length,
    };
  }

  const settled = async (promise: Promise<void>) => {
    let done = false;
    void promise.then(() => {
      done = true;
    });
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    return done;
  };

  it("waits for the seeked-to picture, not just for the seek", async () => {
    const video = fakeVideo();
    const promise = seekTo(video, 4);
    expect(video.currentTime).toBe(4);

    video.seeked();
    expect(await settled(promise)).toBe(false);

    // The picture still on screen is a second old: hashing it now is the bug.
    video.present(3);
    expect(await settled(promise)).toBe(false);

    video.present(4);
    expect(await settled(promise)).toBe(true);
  });

  it("gives up waiting rather than hanging when no frame is presented", async () => {
    vi.useFakeTimers();
    try {
      const video = fakeVideo();
      const promise = seekTo(video, 4);
      video.seeked();
      expect(await settled(promise)).toBe(false);

      await vi.advanceTimersByTimeAsync(1000);
      expect(await settled(promise)).toBe(true);
      // And it stopped asking for frames, so a late callback registers nothing further.
      video.present(0);
      expect(video.pending()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("resolves on `seeked` alone where the browser has no frame callback", async () => {
    const video = fakeVideo();
    const without = {
      duration: video.duration,
      currentTime: 0,
      addEventListener: video.addEventListener,
      seeked: video.seeked,
    };
    const promise = seekTo(without, 4);

    without.seeked();
    expect(await settled(promise)).toBe(true);
  });

  it("never seeks past the end", () => {
    const video = fakeVideo(5);
    void seekTo(video, 9);
    expect(video.currentTime).toBeCloseTo(5, 2);
    expect(video.currentTime).toBeLessThan(5);
  });
});
