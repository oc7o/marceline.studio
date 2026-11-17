// @vitest-environment jsdom
/**
 * The hook that watches one upload (issue #59, ADR 0044 amendment 2026-09-18).
 *
 * `@shared/app/uploadStatus` decides *whether* to ask again and *when*; this is the wiring, and
 * what it must not do is keep asking — after the job is settled, while the tab is hidden, or after
 * the screen is gone. Each of those is a request every few seconds for as long as someone leaves a
 * browser open, so each one is a test.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MAX_FAILURES, useUploadStatus } from "./useUploadStatus";

const UPLOAD_ID = `upl_${"1".repeat(32)}`;

let visibility: DocumentVisibilityState = "visible";
const fetchMock = vi.fn();

function answering(status: string): void {
  fetchMock.mockImplementation(() =>
    Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ id: UPLOAD_ID, status, reason: null }),
    }),
  );
}

/** Let the timers and the promises they start run. */
async function tick(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => visibility,
  });
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("watching one upload", () => {
  it("asks nothing at all without an upload to watch", async () => {
    renderHook(() => useUploadStatus(null));
    await tick(30_000);

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("asks straight away and keeps asking while it is being fingerprinted", async () => {
    answering("hashing");
    const { result } = renderHook(() => useUploadStatus(UPLOAD_ID));

    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`/app/api/uploads/${UPLOAD_ID}`);
    expect(result.current.upload?.status).toBe("hashing");

    await tick(5_000);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);

    // And it slows down: a minute of watching is a handful of requests, not sixty. The schedule
    // itself is `pollDelayMs`, tested in `@shared/app/uploadStatus`.
    await tick(60_000);
    expect(fetchMock.mock.calls.length).toBeLessThan(15);
  });

  it("stops for good once the job is settled", async () => {
    answering("done");
    renderHook(() => useUploadStatus(UPLOAD_ID));

    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await tick(120_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not poll a hidden tab, and picks it up again when it is shown", async () => {
    answering("hashing");
    visibility = "hidden";
    renderHook(() => useUploadStatus(UPLOAD_ID));

    await tick(60_000);
    expect(fetchMock).not.toHaveBeenCalled();

    visibility = "visible";
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalled();
  });

  it("stops when the screen goes away", async () => {
    answering("hashing");
    const { unmount } = renderHook(() => useUploadStatus(UPLOAD_ID));

    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    unmount();
    await tick(60_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("gives up after a few answers it cannot read, and says so", async () => {
    fetchMock.mockImplementation(() => Promise.reject(new Error("offline")));
    const { result } = renderHook(() => useUploadStatus(UPLOAD_ID));

    await tick(60_000);
    expect(fetchMock).toHaveBeenCalledTimes(MAX_FAILURES);
    expect(result.current.unreachable).toBe(true);
    expect(result.current.upload).toBeNull();
  });

  it("forgets a hiccup once an answer arrives", async () => {
    fetchMock.mockImplementationOnce(() => Promise.reject(new Error("offline")));
    answering("hashing");
    const { result } = renderHook(() => useUploadStatus(UPLOAD_ID));

    await tick(2_000);
    expect(result.current.unreachable).toBe(false);
    expect(result.current.upload?.status).toBe("hashing");
  });
});
