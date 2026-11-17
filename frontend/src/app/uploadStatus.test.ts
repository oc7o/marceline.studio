/**
 * What the screens derive from an upload's status (issue #59, ADR 0044 amendment 2026-09-18).
 *
 * The polling schedule is here rather than in the hook on purpose: "stop on a terminal state" and
 * "do not poll a hidden tab" are decisions, and a decision is cheaper to keep correct as a
 * function than as an effect. The words are checked in both languages, because a code the API can
 * answer and the resources cannot word reaches a customer as an English identifier.
 */
import { describe, expect, it } from "vitest";

import { customerDe, customerEn } from "../i18n/resources";
import {
  type Upload,
  type UploadState,
  POLL_DELAYS_MS,
  TERMINAL_STATES,
  UPLOAD_FAILURE_REASONS,
  UPLOAD_STATES,
  UPLOAD_STATUS_KEYS,
  anyRunning,
  bytesPercent,
  framesPercent,
  isAdded,
  isTerminal,
  openUploads,
  pollDelayMs,
  reasonKey,
  saysDeleted,
  shouldPoll,
  statusKey,
} from "./uploadStatus";

function upload(overrides: Partial<Upload> = {}): Upload {
  return {
    id: `upl_${"1".repeat(32)}`,
    title: "An original",
    status: "hashing",
    code: "",
    reason: null,
    original_id: null,
    declared_bytes: 1000,
    received_bytes: 1000,
    chunk_bytes: 100,
    next_chunk: 10,
    frames_done: 0,
    frames_total: null,
    bytes_deleted: false,
    created_at: new Date().toISOString(),
    ...overrides,
  } as Upload;
}

describe("when to ask again", () => {
  it("stops on every terminal state", () => {
    for (const status of UPLOAD_STATES) {
      expect(shouldPoll({ status, hidden: false })).toBe(!isTerminal(status));
    }
    expect(TERMINAL_STATES).toEqual(["done", "refused", "failed", "expired", "cancelled"]);
  });

  it("does not poll a hidden tab, whatever the state", () => {
    for (const status of UPLOAD_STATES) {
      expect(shouldPoll({ status, hidden: true })).toBe(false);
    }
    // Before the first answer there is no state, and a hidden tab still waits.
    expect(shouldPoll({ status: null, hidden: true })).toBe(false);
    expect(shouldPoll({ status: null, hidden: false })).toBe(true);
  });

  it("asks quickly at first and then slows down, and never faster again", () => {
    const delays = POLL_DELAYS_MS.map((_, attempt) => pollDelayMs(attempt));
    expect(delays[0]).toBeLessThanOrEqual(1_000);
    expect([...delays]).toEqual([...delays].sort((a, b) => a - b));
    // Past the end of the table it stays at the slowest value rather than growing for ever.
    expect(pollDelayMs(999)).toBe(delays[delays.length - 1]);
    expect(pollDelayMs(-1)).toBe(delays[0]);
  });
});

describe("the bar", () => {
  it("is indeterminate while nothing knows the total", () => {
    expect(framesPercent(upload({ frames_total: null, frames_done: 0 }))).toBeNull();
    expect(framesPercent(upload({ frames_total: 0, frames_done: 5 }))).toBeNull();
  });

  it("is a whole percent once there is a denominator, and never above 100", () => {
    expect(framesPercent(upload({ frames_total: 200, frames_done: 50 }))).toBe(25);
    // The total is an estimate from the declared duration, so the real count can exceed it.
    expect(framesPercent(upload({ frames_total: 100, frames_done: 140 }))).toBe(100);
  });

  it("counts the bytes of the upload itself the same way", () => {
    expect(bytesPercent(upload({ declared_bytes: 400, received_bytes: 100 }))).toBe(25);
    expect(bytesPercent(upload({ declared_bytes: 0, received_bytes: 0 }))).toBe(0);
  });
});

describe("what became of it", () => {
  it("is only added when there is an original to open", () => {
    expect(isAdded(upload({ status: "done", original_id: `orig_${"2".repeat(32)}` }))).toBe(true);
    expect(isAdded(upload({ status: "done", original_id: null }))).toBe(false);
    expect(isAdded(upload({ status: "hashing" }))).toBe(false);
  });

  it("only says the file was deleted where the server says it verifiably is", () => {
    expect(saysDeleted(upload({ status: "done", bytes_deleted: true }))).toBe(true);
    expect(saysDeleted(upload({ status: "done", bytes_deleted: false }))).toBe(false);
    // Still running: the file is still here, and the screen may not claim otherwise.
    expect(saysDeleted(upload({ status: "hashing", bytes_deleted: true }))).toBe(false);
  });

  it("words a failure by its reason and everything else by its state", () => {
    expect(statusKey(upload({ status: "failed", reason: "decode_failed" }))).toBe(
      "addOriginal.upload.reasons.decode_failed",
    );
    expect(statusKey(upload({ status: "queued" }))).toBe("addOriginal.upload.state.queued");
  });

  it("never builds a key out of a reason it does not know", () => {
    expect(reasonKey("something_new" as never)).toBe("addOriginal.upload.reasons.internal");
  });
});

describe("what the overview lists", () => {
  const day = 24 * 60 * 60 * 1000;
  const now = new Date("2026-09-18T12:00:00Z");
  const at = (days: number) => new Date(now.getTime() - days * day).toISOString();

  it("keeps what is still moving and recent failures, and drops the rest", () => {
    const rows = [
      upload({ id: "upl_a", status: "hashing", created_at: at(0) }),
      upload({ id: "upl_b", status: "failed", reason: "internal", created_at: at(1) }),
      upload({ id: "upl_c", status: "failed", reason: "internal", created_at: at(30) }),
      upload({ id: "upl_d", status: "done", created_at: at(0) }),
      upload({ id: "upl_e", status: "cancelled", reason: "cancelled", created_at: at(0) }),
    ];
    expect(openUploads(rows, now).map((row) => row.id)).toEqual(["upl_a", "upl_b"]);
  });

  it("shows at most a handful", () => {
    const rows = Array.from({ length: 12 }, (_, index) =>
      upload({ id: `upl_${index}`, status: "queued", created_at: at(0) }),
    );
    expect(openUploads(rows, now)).toHaveLength(5);
  });

  it("asks the server again only while something is still moving", () => {
    expect(anyRunning([upload({ status: "queued" })])).toBe(true);
    expect(anyRunning([upload({ status: "failed", reason: "internal" })])).toBe(false);
    expect(anyRunning([])).toBe(false);
  });
});

describe("the words", () => {
  const lookup = (resources: unknown, path: string): unknown =>
    path.split(".").reduce<unknown>((value, key) => {
      if (value && typeof value === "object") return (value as Record<string, unknown>)[key];
      return undefined;
    }, resources);

  it.each([
    ["en", customerEn],
    ["de", customerDe],
  ])("has a sentence in %s for every state and every reason", (_language, resources) => {
    const missing = UPLOAD_STATUS_KEYS.filter((key) => typeof lookup(resources, key) !== "string");
    expect(missing).toEqual([]);
  });

  it("covers every state and reason the API can answer", () => {
    for (const state of UPLOAD_STATES) {
      expect(UPLOAD_STATUS_KEYS).toContain(`addOriginal.upload.state.${state}`);
    }
    for (const reason of UPLOAD_FAILURE_REASONS) {
      expect(UPLOAD_STATUS_KEYS).toContain(`addOriginal.upload.reasons.${reason}`);
    }
  });

  it("promises no duration in either language, which is what this screen would want", () => {
    for (const resources of [customerEn, customerDe]) {
      for (const key of UPLOAD_STATUS_KEYS) {
        const text = String(lookup(resources, key));
        expect(text).not.toMatch(/within \d+|innerhalb|garant|guarantee|expect|wahrscheinlich/i);
      }
    }
  });

  it("states the file's fate only where it is a fact", () => {
    // The sentence exists, and `saysDeleted` above is the only thing that renders it.
    expect(String(lookup(customerEn, "addOriginal.upload.watch.deleted"))).toMatch(/deleted/i);
    expect(String(lookup(customerDe, "addOriginal.upload.watch.deleted"))).toMatch(/gelöscht/i);
  });

  it("offers hashing on the customer's own computer as the way out of a failure", () => {
    expect(String(lookup(customerEn, "addOriginal.upload.watch.fallback"))).toMatch(
      /on this computer/i,
    );
    expect(String(lookup(customerEn, "addOriginal.upload.reasons.unsupported_format"))).toMatch(
      /on this computer/i,
    );
    expect(String(lookup(customerDe, "addOriginal.upload.reasons.unsupported_format"))).toMatch(
      /auf diesem Computer/i,
    );
  });
});

/** A state the API could answer that nothing above walks would be a silent hole. */
it("names every state the schema declares", () => {
  const states: UploadState[] = [
    "receiving",
    "queued",
    "hashing",
    "done",
    "refused",
    "failed",
    "expired",
    "cancelled",
  ];
  expect([...UPLOAD_STATES]).toEqual(states);
});
