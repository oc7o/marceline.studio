// @vitest-environment jsdom
/**
 * The "Add an original" page against a stubbed customer API and a stubbed fingerprinting client:
 * what it offers, what it refuses to start without, what it sends, and how it words each outcome.
 *
 * The fingerprinting client is replaced (a real browser decode is `make browser-parity`'s job); the
 * document it returns is built by the real `buildFingerprint` from hashes generated here, so the
 * request body is exactly what the page would send. No media anywhere.
 */
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setLanguageForTest } from "../i18n";
import { de } from "../i18n/de";
import { en } from "../i18n/en";
import type { FingerprintOptions } from "../lib/fingerprintClient";
import { MAX_UPLOAD_POSITIONS, ORIENTATIONS, buildFingerprint, frameDocuments } from "../lib/videoFingerprint";
import { RIGHTS_TEXT_VERSION } from "./addOriginal";
import { ACCOUNT, type Reply, original, originalId, remoteLoaders, renderAt, serveCustomer } from "./testApi";

// Load-bearing, see customerApp.test.tsx: openapi-fetch captures fetch at import time, and Node's
// Request refuses the relative URLs the SPA uses on purpose.
const { fetchMock, fingerprintFile } = vi.hoisted(() => {
  const NativeRequest = globalThis.Request;
  class RelativeRequest extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  }
  globalThis.Request = RelativeRequest;
  const fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  return { fetchMock, fingerprintFile: vi.fn() };
});

vi.mock("../lib/fingerprintClient", () => ({
  fingerprintFile,
  failureKind: (error: unknown) =>
    error instanceof Error && error.name === "CodecUnsupportedError" ? "codec_unsupported" : "client_failed",
}));

interface Posted {
  path: string;
  body: Record<string, unknown>;
}

let posted: Posted[] = [];

/** Signed-in customer who may add originals, plus canned answers for the two POST endpoints. */
function serve(posts: Record<string, Reply>, canAdd = true) {
  serveCustomer(fetchMock, { "/api/v1/app/me": { status: 200, body: { ...ACCOUNT, can_add_originals: canAdd } } });
  const reads = fetchMock.getMockImplementation();
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    if (request.method !== "POST") return reads?.(input, init);
    const path = new URL(request.url).pathname;
    posted.push({ path, body: (await request.json()) as Record<string, unknown> });
    const reply = posts[path] ?? { status: 404, body: { detail: "unexpected" } };
    return new Response(JSON.stringify(reply.body), {
      status: reply.status,
      headers: { "content-type": "application/json" },
    });
  });
}

/** A fingerprinting run over a generated 3-second clip: progress, then a real document. */
function succeedWithGeneratedHashes() {
  fingerprintFile.mockImplementation(async (file: File, options: FingerprintOptions) => {
    const hashes = Array.from({ length: ORIENTATIONS }, (_, i) => i.toString(16).padStart(2, "0").repeat(32));
    options.onProgress?.({ stage: "hash", done: 1, total: 3, bytesDigested: file.size, fileSize: file.size, framesHashed: 1, framesTotal: 3, elapsedMs: 100 });
    return buildFingerprint({
      sha256: "ab".repeat(32),
      fileSize: file.size,
      durationS: 3,
      fps: options.fps ?? 1,
      title: options.title ?? "",
      creators: options.creators ?? [],
      frames: [0, 1, 2].flatMap((index) => frameDocuments(index, options.fps ?? 1, 90, hashes)),
      client: { tool: "marceline-browser 0.2.0" },
    });
  });
}

const CREATED = {
  status: 201,
  body: { original: original({ id: originalId(42), title: "Beach day", state: "nothing_found", counts: { confirmed: 0, removed: 0 } }), created: true },
};

async function fillForm({ confirm = true }: { confirm?: boolean } = {}) {
  const input = await screen.findByLabelText(en.addOriginal.file.label, {});
  const video = new File([new Uint8Array([0, 1, 2, 3])], "Beach day.mp4", { type: "video/mp4" });
  fireEvent.change(input, { target: { files: [video] } });
  fireEvent.change(screen.getByLabelText(en.addOriginal.rightsHolder.label), { target: { value: " Sasha  Meyer " } });
  fireEvent.change(screen.getByLabelText(en.addOriginal.others.label), { target: { value: "Carla Bremer\n\n" } });
  if (confirm) fireEvent.click(screen.getByLabelText(en.addOriginal.rights.statement));
}

beforeEach(async () => {
  fetchMock.mockReset();
  fingerprintFile.mockReset();
  posted = [];
  window.localStorage.clear();
  document.cookie = "csrftoken=token";
  await setLanguageForTest("en");
});

afterEach(() => {
  cleanup();
});

describe("add an original", () => {
  it("is offered on the overview only where the account may add originals", async () => {
    serve({});
    renderAt("/app");
    const link = await screen.findByRole("link", { name: en.overview.addOriginal });
    expect(link.getAttribute("href")).toBe("/app/originals/new");
    cleanup();

    serve({}, false);
    renderAt("/app");
    await screen.findByText(en.overview.empty, {});
    expect(screen.queryByRole("link", { name: en.overview.addOriginal })).toBeNull();
  });

  it("says so, and shows no form, where adding is not available", async () => {
    serve({}, false);
    renderAt("/app/originals/new");

    expect(await screen.findByText(en.addOriginal.unavailable, {})).toBeTruthy();
    expect(screen.queryByLabelText(en.addOriginal.file.label)).toBeNull();
  });

  it("does not start without a file, a rights holder and the declaration", async () => {
    serve({});
    renderAt("/app/originals/new");

    fireEvent.click(await screen.findByRole("button", { name: en.addOriginal.start }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(en.addOriginal.problems.missingFile);
    expect(alert.textContent).toContain(en.addOriginal.problems.missingRightsHolder);
    expect(alert.textContent).toContain(en.addOriginal.problems.notConfirmed);
    expect(fingerprintFile).not.toHaveBeenCalled();
    expect(posted).toEqual([]);
  });

  it("fingerprints locally and sends hashes, metadata and the declaration — never the file", async () => {
    serve({ "/api/v1/app/fingerprints": CREATED });
    succeedWithGeneratedHashes();
    const { container } = renderAt("/app/originals/new");

    await fillForm();
    // The title defaults to the file name without its extension, like the CLI.
    expect((screen.getByLabelText(en.addOriginal.title.label) as HTMLInputElement).value).toBe("Beach day");
    fireEvent.click(screen.getByRole("button", { name: en.addOriginal.start }));

    expect(await screen.findByText(en.addOriginal.done.created.replace("{{title}}", "Beach day"), {})).toBeTruthy();
    const [options] = fingerprintFile.mock.calls.map((call) => call[1] as FingerprintOptions);
    expect(options?.fps).toBe(1);
    expect(options?.maxPositions).toBe(MAX_UPLOAD_POSITIONS);
    expect(options?.creators).toEqual(["Sasha Meyer", "Carla Bremer"]);

    expect(posted.map((entry) => entry.path)).toEqual(["/api/v1/app/fingerprints"]);
    const body = posted[0]?.body ?? {};
    expect(body.rights_confirmed).toBe(true);
    expect(body.rights_text_version).toBe(RIGHTS_TEXT_VERSION);
    expect(body.language).toBe("en");
    const fingerprint = body.fingerprint as Record<string, unknown>;
    expect(fingerprint.creator).toEqual(["Sasha Meyer", "Carla Bremer"]);
    expect(fingerprint.kind).toBe("video");
    expect(JSON.stringify(body)).not.toContain("Beach day.mp4");

    const open = screen.getByRole("link", { name: en.addOriginal.done.open });
    // The customer's opaque id (ADR 0033), exactly as the API answered it.
    expect(open.getAttribute("href")).toBe(`/app/originals/${originalId(42)}`);
    expect(remoteLoaders(container)).toEqual([]);
  });

  it("says when the video is already in the account", async () => {
    serve({ "/api/v1/app/fingerprints": { status: 200, body: { ...CREATED.body, created: false } } });
    succeedWithGeneratedHashes();
    renderAt("/app/originals/new");

    await fillForm();
    fireEvent.click(screen.getByRole("button", { name: en.addOriginal.start }));

    expect(await screen.findByText(en.addOriginal.done.existing.replace("{{title}}", "Beach day"), {})).toBeTruthy();
  });

  it("words a claim that needs a human check without naming anyone, in German too", async () => {
    await setLanguageForTest("de");
    serve({ "/api/v1/app/fingerprints": { status: 409, body: { detail: "claim_needs_review", message: "" } } });
    succeedWithGeneratedHashes();
    renderAt("/app/originals/new");

    const input = await screen.findByLabelText(de.addOriginal.file.label, {});
    fireEvent.change(input, { target: { files: [new File([new Uint8Array([7])], "clip.mov", { type: "video/quicktime" })] } });
    fireEvent.change(screen.getByLabelText(de.addOriginal.rightsHolder.label), { target: { value: "Sasha Meyer" } });
    fireEvent.click(screen.getByLabelText(de.addOriginal.rights.statement));
    fireEvent.click(screen.getByRole("button", { name: de.addOriginal.start }));

    expect(await screen.findByText(de.addOriginal.errors.server.claim_needs_review, {})).toBeTruthy();
    expect(posted[0]?.body.language).toBe("de");
  });

  it("explains a video the browser cannot read and counts it without the file name", async () => {
    serve({ "/api/v1/app/fingerprints/client-events": { status: 200, body: { recorded: true } } });
    fingerprintFile.mockImplementation(async () => {
      const error = new Error("This browser cannot decode the file");
      error.name = "CodecUnsupportedError";
      throw error;
    });
    renderAt("/app/originals/new");

    await fillForm();
    fireEvent.click(screen.getByRole("button", { name: en.addOriginal.start }));

    expect(await screen.findByText(en.addOriginal.errors.codecUnsupported, {})).toBeTruthy();
    await waitFor(() => expect(posted.map((entry) => entry.path)).toEqual(["/api/v1/app/fingerprints/client-events"]));
    expect(posted[0]?.body).toMatchObject({ kind: "codec_unsupported", container: "video/mp4" });
    expect(JSON.stringify(posted[0]?.body)).not.toContain("Beach day");
  });

  it("says an upload production accepted is being added, and the overview follows it", async () => {
    const submission = {
      id: `sub_${"5".repeat(32)}`,
      title: "Beach day",
      status: "processing",
      code: "",
      original_id: null,
      received_at: "2026-09-17T10:00:00Z",
    };
    serve({ "/api/v1/app/fingerprints": { status: 202, body: { submission } } });
    succeedWithGeneratedHashes();
    renderAt("/app/originals/new");

    await fillForm();
    fireEvent.click(screen.getByRole("button", { name: en.addOriginal.start }));

    expect(await screen.findByText(en.addOriginal.done.received.replace("{{title}}", "Beach day"), {})).toBeTruthy();
    expect(screen.queryByRole("link", { name: en.addOriginal.done.open })).toBeNull();
    expect(screen.getByRole("link", { name: en.addOriginal.done.overview }).getAttribute("href")).toBe("/app");
  });

  it("lists uploads being added and not added on the overview, in German too", async () => {
    await setLanguageForTest("de");
    const processing = {
      id: `sub_${"6".repeat(32)}`,
      title: "Strandtag",
      status: "processing",
      code: "",
      original_id: null,
      received_at: "2026-09-17T10:00:00Z",
    };
    const refused = { ...processing, id: `sub_${"7".repeat(32)}`, title: "Zweitvideo", status: "refused", code: "claim_needs_review" };
    const added = { ...processing, id: `sub_${"8".repeat(32)}`, title: "Schon da", status: "done", code: "created" };
    serveCustomer(fetchMock, {
      "/api/v1/app/me": { status: 200, body: { ...ACCOUNT, can_add_originals: true } },
      "/api/v1/app/fingerprints/submissions": { status: 200, body: [processing, refused, added] },
    });
    const { container } = renderAt("/app");

    const list = await screen.findByRole("region", { name: de.addOriginal.submissions.heading });
    expect(list.textContent).toContain("Strandtag");
    expect(list.textContent).toContain(de.addOriginal.submissions.status.processing);
    expect(list.textContent).toContain(de.addOriginal.submissions.processing);
    expect(list.textContent).toContain(de.addOriginal.errors.server.claim_needs_review);
    expect(list.textContent).not.toContain("Schon da");
    expect(remoteLoaders(container)).toEqual([]);
  });

  it("says when adding is paused on production and nothing was saved", async () => {
    serve({ "/api/v1/app/fingerprints": { status: 503, body: { detail: "processing_paused", message: "" } } });
    succeedWithGeneratedHashes();
    renderAt("/app/originals/new");

    await fillForm();
    fireEvent.click(screen.getByRole("button", { name: en.addOriginal.start }));

    expect(await screen.findByText(en.addOriginal.errors.server.processing_paused, {})).toBeTruthy();
  });

  it("does no work before the customer starts", async () => {
    serve({});
    renderAt("/app/originals/new");
    await fillForm({ confirm: false });

    expect(fingerprintFile).not.toHaveBeenCalled();
  });
});
