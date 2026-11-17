// @vitest-environment jsdom
/**
 * "Add an original" in the app production serves (issue #29, ADR 0036).
 *
 * The SPA's copy of this screen has had `src/app/addOriginalScreen.test.tsx` since it was built;
 * this is its half for the Next island, and it asks the same questions: what it refuses to start
 * without, **what leaves the computer**, and how it words each outcome. It is an island rather
 * than a server component, so unlike `screens.test.tsx` it renders into jsdom.
 *
 * The fingerprinting client is stubbed — a real decode is `make browser-parity`'s job — but the
 * document it returns is built by the real `buildFingerprint`, so the body the screen sends is
 * exactly the body it would send. No media anywhere, on either side.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RIGHTS_TEXT_VERSION, UPLOAD_DECLARATION_VERSION } from "@shared/app/addOriginal";
import { de } from "@shared/i18n/de";
import { en } from "@shared/i18n/en";
import { ApiError } from "@shared/api/errors";
import type { Upload } from "@shared/app/uploadStatus";
import type { FingerprintOptions } from "@shared/lib/fingerprintClient";
import { buildPhotoFingerprint } from "@shared/lib/photoFingerprint";
import {
  MAX_UPLOAD_POSITIONS,
  ORIENTATIONS,
  buildFingerprint,
  frameDocuments,
} from "@shared/lib/videoFingerprint";

import { type Resource, slice } from "../lib/translate";
import { AddOriginalScreen } from "./AddOriginalScreen";

const { send, fingerprintFile, fingerprintPhotos, uploadFile, useUploadStatus } = vi.hoisted(
  () => ({
    send: vi.fn(),
    fingerprintFile: vi.fn(),
    fingerprintPhotos: vi.fn(),
    uploadFile: vi.fn(),
    useUploadStatus: vi.fn(),
  }),
);

vi.mock("../lib/write", () => ({ send }));
// Cloud fingerprinting (ADR 0044): the sending itself is `uploadMedia.test`'s and the polling is
// `useUploadStatus.test`'s; here the question is what the customer reads in each state.
vi.mock("../lib/uploadMedia", () => ({
  uploadFile,
  UploadCancelled: class UploadCancelled extends Error {
    override name = "UploadCancelled";
  },
}));
vi.mock("../lib/useUploadStatus", () => ({ useUploadStatus, MAX_FAILURES: 5 }));
vi.mock("@shared/lib/fingerprintClient", () => ({
  fingerprintFile,
  fingerprintPhotos,
  failureKind: (error: unknown) =>
    error instanceof Error && error.name === "CodecUnsupportedError"
      ? "codec_unsupported"
      : "client_failed",
}));

const WORDS = {
  en: slice(en as unknown as Resource, "addOriginal", "common"),
  de: slice(de as unknown as Resource, "addOriginal", "common"),
};

const ORIGINAL_ID = `orig_${"4".repeat(32)}`;
const UPLOAD_ID = `upl_${"7".repeat(32)}`;

function renderScreen(language: "en" | "de" = "en") {
  return render(<AddOriginalScreen language={language} words={WORDS[language]} />);
}

/**
 * The screen as a customer sees it where cloud fingerprinting is switched on (ADR 0044). Its words
 * are part of the `addOriginal` slice (issue #99), so they arrive with the rest of this screen's
 * words — the same resource `requestI18n()` builds at request time.
 */
function renderWithUpload(language: "en" | "de" = "en") {
  return render(
    <AddOriginalScreen language={language} words={WORDS[language]} canUploadMedia />,
  );
}

/** One posted write, as `lib/write.send` receives it. */
function posted(): { path: string; body: Record<string, unknown> }[] {
  return send.mock.calls.map((call) => ({
    path: String(call[0]),
    body: (call[2] ?? {}) as Record<string, unknown>,
  }));
}

/** A fingerprinting run over a generated 3-second clip: progress, then a real document. */
function succeedWithGeneratedHashes() {
  fingerprintFile.mockImplementation(async (file: File, options: FingerprintOptions) => {
    const hashes = Array.from({ length: ORIENTATIONS }, (_, index) =>
      index.toString(16).padStart(2, "0").repeat(32),
    );
    options.onProgress?.({
      stage: "hash",
      done: 1,
      total: 3,
      bytesDigested: file.size,
      fileSize: file.size,
      framesHashed: 1,
      framesTotal: 3,
      elapsedMs: 100,
    });
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

function fillForm(language: "en" | "de" = "en", { confirm = true }: { confirm?: boolean } = {}) {
  const words = language === "en" ? en : de;
  const video = new File([new Uint8Array([0, 1, 2, 3])], "Beach day.mp4", { type: "video/mp4" });
  fireEvent.change(screen.getByLabelText(words.addOriginal.file.label), {
    target: { files: [video] },
  });
  fireEvent.change(screen.getByLabelText(words.addOriginal.rightsHolder.label), {
    target: { value: " Sasha  Meyer " },
  });
  fireEvent.change(screen.getByLabelText(words.addOriginal.others.label), {
    target: { value: "Carla Bremer\n\n" },
  });
  if (confirm) fireEvent.click(screen.getByLabelText(words.addOriginal.rights.statement));
}

/**
 * Tick the upload declaration (ADR 0044 amendment 2026-09-19). Only shown once "send the file" is
 * chosen, and cleared again by changing the method or the file — so it is always ticked last.
 */
function declare(language: "en" | "de" = "en") {
  const words = (language === "en" ? en : de).addOriginal.upload.declaration;
  fireEvent.click(screen.getByLabelText(words.solePerformer));
  fireEvent.click(screen.getByLabelText(words.article9Consent));
}

const start = (language: "en" | "de" = "en") =>
  fireEvent.click(
    screen.getByRole("button", {
      name: (language === "en" ? en : de).addOriginal.start,
    }),
  );

beforeEach(() => {
  send.mockReset();
  send.mockResolvedValue({ original: { id: ORIGINAL_ID, title: "Beach day" }, created: true });
  fingerprintFile.mockReset();
  fingerprintPhotos.mockReset();
  uploadFile.mockReset();
  uploadFile.mockResolvedValue(anUpload({ status: "queued" }));
  useUploadStatus.mockReset();
  useUploadStatus.mockReturnValue({ upload: null, unreachable: false });
});

/** One job row as `GET /api/v1/app/uploads/{id}` answers it. */
function anUpload(overrides: Partial<Upload> = {}): Upload {
  return {
    id: UPLOAD_ID,
    title: "Beach day",
    status: "queued",
    code: "",
    reason: null,
    original_id: null,
    declared_bytes: 4,
    received_bytes: 4,
    chunk_bytes: 4,
    next_chunk: 1,
    frames_done: 0,
    frames_total: null,
    bytes_deleted: false,
    created_at: "2026-09-18T12:00:00Z",
    ...overrides,
  } as Upload;
}

afterEach(cleanup);

describe("add an original", () => {
  it("does not start without a file, a rights holder and the declaration", async () => {
    renderScreen();
    start();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(en.addOriginal.problems.missingFile);
    expect(alert.textContent).toContain(en.addOriginal.problems.missingRightsHolder);
    expect(alert.textContent).toContain(en.addOriginal.problems.notConfirmed);
    expect(fingerprintFile).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  });

  it("does no work before the customer starts", () => {
    renderScreen();
    fillForm("en", { confirm: false });

    expect(fingerprintFile).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  });

  it("fingerprints locally and sends hashes, metadata and the declaration — never the file", async () => {
    succeedWithGeneratedHashes();
    const { container } = renderScreen();

    fillForm();
    // The title defaults to the file name without its extension, like the CLI.
    expect(screen.getByLabelText(en.addOriginal.title.label)).toHaveProperty("value", "Beach day");
    start();

    expect(
      await screen.findByText(en.addOriginal.done.created.replace("{{title}}", "Beach day")),
    ).toBeTruthy();

    const options = fingerprintFile.mock.calls.map((call) => call[1] as FingerprintOptions)[0];
    expect(options?.fps).toBe(1);
    expect(options?.maxPositions).toBe(MAX_UPLOAD_POSITIONS);
    expect(options?.creators).toEqual(["Sasha Meyer", "Carla Bremer"]);

    const writes = posted();
    expect(writes.map((write) => write.path)).toEqual(["/app/api/fingerprints"]);
    const body = writes[0]?.body ?? {};
    expect(body.rights_confirmed).toBe(true);
    expect(body.rights_text_version).toBe(RIGHTS_TEXT_VERSION);
    expect(body.language).toBe("en");
    const fingerprint = body.fingerprint as Record<string, unknown>;
    expect(fingerprint.kind).toBe("video");
    expect(fingerprint.creator).toEqual(["Sasha Meyer", "Carla Bremer"]);
    // The file itself, and even its name, stay on the computer.
    expect(JSON.stringify(body)).not.toContain("Beach day.mp4");

    expect(
      screen.getByRole("link", { name: en.addOriginal.done.open }).getAttribute("href"),
    ).toBe(`/app/originals/${ORIGINAL_ID}`);
    // Nothing on this screen is able to render a copy of the video (issue #53).
    expect(container.querySelectorAll("img, video, canvas, picture, iframe")).toHaveLength(0);
  });

  /**
   * A long run says how long is left, measured on this device (issue #29).
   *
   * The wait is minutes on a desktop and can be most of an hour on a phone
   * (`docs/research/browser-fingerprinting-speed.md`), so a bar alone does not tell a creator
   * whether to stay on the page. `Date.now` is stepped by hand rather than by a timer: the estimate
   * is a function of elapsed time, and a real clock would make this test flaky by exactly the
   * amount it measures.
   */
  it("says how much longer it will take on this device, once it can", async () => {
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    // No clock anywhere: the run reports its own elapsed time, which is exactly why a component
    // never has to read one (`react-hooks/purity`) and why this test cannot be flaky.
    fingerprintFile.mockImplementation(async (file: File, options: FingerprintOptions) => {
      // 60 of 600 positions in 30 s → 500 ms each → 540 left → 270 s → "about 5 minutes".
      options.onProgress?.({
        stage: "hash",
        done: 60,
        total: 600,
        bytesDigested: file.size,
        fileSize: file.size,
        framesHashed: 60,
        framesTotal: 600,
        elapsedMs: 30_000,
      });
      await held;
      throw new Error("never finishes in this test");
    });
    renderScreen();
    fillForm();
    start();

    expect(
      await screen.findByText(en.addOriginal.progress.eta_other.replace("{{count}}", "5")),
    ).toBeTruthy();
    // And the sentence that makes the estimate actionable is still there.
    expect(screen.getByText(en.addOriginal.progress.keepOpen)).toBeTruthy();
    release();
  });

  it("says nothing about the time left until the run has said enough", async () => {
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    fingerprintFile.mockImplementation(async (file: File, options: FingerprintOptions) => {
      // Three positions in: too few to extrapolate from, so no number is shown at all.
      options.onProgress?.({
        stage: "hash",
        done: 3,
        total: 600,
        bytesDigested: 0,
        fileSize: file.size,
        framesHashed: 3,
        framesTotal: 600,
        elapsedMs: 30_000,
      });
      await held;
      throw new Error("never finishes in this test");
    });
    renderScreen();
    fillForm();
    start();

    const counted = en.addOriginal.progress.framesCount
      .replace("{{done}}", "3")
      .replace("{{total}}", "600");
    expect(await screen.findByText(new RegExp(counted))).toBeTruthy();
    expect(screen.queryByText(/minute/)).toBeNull();
    release();
  });

  it("says when the video is already in the account", async () => {
    succeedWithGeneratedHashes();
    send.mockResolvedValue({ original: { id: ORIGINAL_ID, title: "Beach day" }, created: false });
    renderScreen();

    fillForm();
    start();

    expect(
      await screen.findByText(en.addOriginal.done.existing.replace("{{title}}", "Beach day")),
    ).toBeTruthy();
  });

  it("says an upload production accepted is being added, and links back to the overview", async () => {
    succeedWithGeneratedHashes();
    send.mockResolvedValue({
      submission: {
        id: `sub_${"5".repeat(32)}`,
        title: "Beach day",
        status: "processing",
        detail: null,
        created_at: "2026-09-17T10:00:00Z",
        settled_at: null,
      },
    });
    renderScreen();

    fillForm();
    start();

    expect(
      await screen.findByText(en.addOriginal.done.received.replace("{{title}}", "Beach day")),
    ).toBeTruthy();
    expect(screen.queryByRole("link", { name: en.addOriginal.done.open })).toBeNull();
    expect(
      screen.getByRole("link", { name: en.addOriginal.done.overview }).getAttribute("href"),
    ).toBe("/app");
  });

  it("words a claim that needs a human check without naming anyone, in German too", async () => {
    succeedWithGeneratedHashes();
    send.mockRejectedValue(
      new ApiError(409, "claim_needs_review", { detail: "claim_needs_review" }, null),
    );
    renderScreen("de");

    fillForm("de");
    start("de");

    expect(
      await screen.findByText(de.addOriginal.errors.server.claim_needs_review),
    ).toBeTruthy();
    expect(posted()[0]?.body.language).toBe("de");
  });

  it("explains a video the browser cannot read and counts it without the file name", async () => {
    fingerprintFile.mockImplementation(() => {
      const error = new Error("This browser cannot decode the file");
      error.name = "CodecUnsupportedError";
      return Promise.reject(error);
    });
    renderScreen();

    fillForm();
    start();

    expect(await screen.findByText(en.addOriginal.errors.codecUnsupported)).toBeTruthy();
    await waitFor(() =>
      expect(posted().map((write) => write.path)).toEqual([
        "/app/api/fingerprints/client-events",
      ]),
    );
    const event = posted()[0]?.body ?? {};
    expect(event).toMatchObject({ kind: "codec_unsupported", container: "video/mp4" });
    expect(JSON.stringify(event)).not.toContain("Beach day");
  });

  it("says nothing was saved when adding is paused on production", async () => {
    succeedWithGeneratedHashes();
    send.mockRejectedValue(
      new ApiError(503, "processing_paused", { detail: "processing_paused" }, null),
    );
    renderScreen();

    fillForm();
    start();

    expect(await screen.findByText(en.addOriginal.errors.server.processing_paused)).toBeTruthy();
  });
});

describe("sending the file instead of hashing it (ADR 0044)", () => {
  /**
   * The two method radios, in the order they are rendered: local first, upload second.
   *
   * Scoped to the method fieldset by its legend, because the form now has a second radio group —
   * a video or a photo set (issue #116) — and "every radio on the screen" would count both.
   */
  function methods(): HTMLInputElement[] {
    const group = screen.queryByRole("group", { name: en.addOriginal.method.legend });
    return group === null ? [] : (within(group).queryAllByRole("radio") as HTMLInputElement[]);
  }

  it("is not offered at all where the machine does not take files", () => {
    renderScreen();

    expect(methods()).toHaveLength(0);
    expect(screen.queryByText(en.addOriginal.method.upload)).toBeNull();
  });

  it("offers it as a second choice and leaves local hashing selected", () => {
    renderWithUpload();

    expect(screen.getByText(en.addOriginal.method.local)).toBeTruthy();
    expect(screen.getByText(en.addOriginal.method.upload)).toBeTruthy();
    // The default is what the product promises: the video stays on the customer's computer unless
    // they choose otherwise, for this one file.
    expect(methods().map((radio) => radio.checked)).toEqual([true, false]);
    expect(screen.getByRole("button", { name: en.addOriginal.start })).toBeTruthy();
  });

  it("names the file's fate before anything is sent, and promises no duration", () => {
    renderWithUpload();

    const hint = en.addOriginal.method.uploadHint;
    expect(screen.getByText(hint)).toBeTruthy();
    expect(hint).toMatch(/delete the file/);
    expect(hint).not.toMatch(/within \d+ (hours?|days?)|guarantee|expect|likely/i);
  });

  it("changes the button once the customer chooses to send the file", () => {
    renderWithUpload();

    const [, upload] = methods();
    expect(upload).toBeTruthy();
    if (upload) fireEvent.click(upload);

    expect(screen.getByRole("button", { name: en.addOriginal.upload.start })).toBeTruthy();
    expect(screen.queryByRole("button", { name: en.addOriginal.start })).toBeNull();
  });

  /**
   * The declaration (ADR 0044 amendment 2026-09-19). It is the feature's whole consent basis: the
   * uploader is the only person in the file, and consents explicitly to the processing of it. It
   * belongs to the *sending* path only — a file hashed on the customer's own computer is not
   * processed by us at all, so asking there would be asking for nothing.
   */
  it("asks for the declaration only where the file would be sent", () => {
    renderWithUpload();

    expect(screen.queryByText(en.addOriginal.upload.declaration.solePerformer)).toBeNull();
    const [, upload] = methods();
    if (upload) fireEvent.click(upload);

    expect(screen.getByText(en.addOriginal.upload.declaration.solePerformer)).toBeTruthy();
    expect(screen.getByText(en.addOriginal.upload.declaration.article9Consent)).toBeTruthy();
    // The way out for a file with other people in it, on the screen rather than in a refusal.
    expect(screen.getByText(en.addOriginal.upload.declaration.others)).toBeTruthy();
  });

  it("sends nothing until both statements are ticked", () => {
    renderWithUpload();
    fillForm();
    const [, upload] = methods();
    if (upload) fireEvent.click(upload);

    fireEvent.click(screen.getByRole("button", { name: en.addOriginal.upload.start }));

    expect(uploadFile).not.toHaveBeenCalled();
    expect(screen.getByText(en.addOriginal.problems.notDeclared)).toBeTruthy();

    // One tick is not enough: they are two different statements.
    fireEvent.click(screen.getByLabelText(en.addOriginal.upload.declaration.solePerformer));
    fireEvent.click(screen.getByRole("button", { name: en.addOriginal.upload.start }));
    expect(uploadFile).not.toHaveBeenCalled();
  });

  it("forgets the declaration when the method or the file changes", () => {
    renderWithUpload();
    fillForm();
    const [local, upload] = methods();
    if (upload) fireEvent.click(upload);
    declare();

    if (local) fireEvent.click(local);
    if (upload) fireEvent.click(upload);

    const boxes = [
      screen.getByLabelText(en.addOriginal.upload.declaration.solePerformer),
      screen.getByLabelText(en.addOriginal.upload.declaration.article9Consent),
    ] as HTMLInputElement[];
    expect(boxes.map((box) => box.checked)).toEqual([false, false]);
  });

  it("sends the declaration's version with the upload", async () => {
    renderWithUpload();
    fillForm();
    const [, upload] = methods();
    if (upload) fireEvent.click(upload);
    declare();

    fireEvent.click(screen.getByRole("button", { name: en.addOriginal.upload.start }));

    await waitFor(() => expect(uploadFile).toHaveBeenCalled());
    const [, options] = uploadFile.mock.calls[0] as [File, { uploadDeclarationVersion: string }];
    expect(options.uploadDeclarationVersion).toBe(UPLOAD_DECLARATION_VERSION);
  });
});

describe("what the customer reads after the file is sent (ADR 0044 amendment)", () => {
  const words = en.addOriginal.upload;

  /** Fill the form, choose "send the file", start — the screen is then watching the job. */
  async function sendFile(language: "en" | "de" = "en") {
    renderWithUpload(language);
    fillForm(language);
    const legend = (language === "en" ? en : de).addOriginal.method.legend;
    const group = screen.getByRole("group", { name: legend });
    const [, upload] = within(group).queryAllByRole("radio") as HTMLInputElement[];
    if (upload) fireEvent.click(upload);
    declare(language);
    fireEvent.click(
      screen.getByRole("button", {
        name: (language === "en" ? en : de).addOriginal.upload.start,
      }),
    );
    await waitFor(() => expect(uploadFile).toHaveBeenCalled());
  }

  it("says it is queued, that the page may be closed, and promises no time", async () => {
    await sendFile();

    expect(await screen.findByText(words.watch.leave)).toBeTruthy();
    expect(screen.getByText(words.state.queued)).toBeTruthy();
    // Nothing anywhere on this screen says when it will be done.
    expect(document.body.textContent ?? "").not.toMatch(/within \d+|minute|hour/i);
  });

  it("shows an indeterminate bar until the file has been probed", async () => {
    useUploadStatus.mockReturnValue({
      upload: anUpload({ status: "hashing", frames_done: 0, frames_total: null }),
      unreachable: false,
    });
    await sendFile();

    const bar = await screen.findByRole("progressbar", { name: words.watch.hashing });
    expect(bar.getAttribute("aria-valuenow")).toBeNull();
    expect(screen.getByText(words.watch.framesWaiting)).toBeTruthy();
  });

  it("shows how far the fingerprinting has come once there is a total", async () => {
    useUploadStatus.mockReturnValue({
      upload: anUpload({ status: "hashing", frames_done: 30, frames_total: 120 }),
      unreachable: false,
    });
    await sendFile();

    const bar = await screen.findByRole("progressbar", { name: words.watch.hashing });
    expect(bar.getAttribute("aria-valuenow")).toBe("25");
    expect(screen.getByText(/30 of 120 moments/)).toBeTruthy();
  });

  it("links to the original once it is added, and says the file has been deleted", async () => {
    useUploadStatus.mockReturnValue({
      upload: anUpload({ status: "done", original_id: ORIGINAL_ID, bytes_deleted: true }),
      unreachable: false,
    });
    await sendFile();

    const link = await screen.findByRole("link", { name: en.addOriginal.done.open });
    expect(link.getAttribute("href")).toBe(`/app/originals/${ORIGINAL_ID}`);
    expect(screen.getByText(words.watch.deleted)).toBeTruthy();
  });

  it("does not claim the file was deleted while the server has not said so", async () => {
    useUploadStatus.mockReturnValue({
      upload: anUpload({ status: "done", original_id: ORIGINAL_ID, bytes_deleted: false }),
      unreachable: false,
    });
    await sendFile();

    await screen.findByRole("link", { name: en.addOriginal.done.open });
    expect(screen.queryByText(words.watch.deleted)).toBeNull();
  });

  it("names the reason it was not added and offers hashing on this computer instead", async () => {
    useUploadStatus.mockReturnValue({
      upload: anUpload({
        status: "refused",
        code: "unsupported_codec",
        reason: "unsupported_format",
        bytes_deleted: true,
      }),
      unreachable: false,
    });
    await sendFile();

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(words.reasons.unsupported_format)).toBeTruthy();
    expect(screen.getByText(words.watch.fallback)).toBeTruthy();
    // The code itself is never shown; the customer reads our sentence.
    expect(document.body.textContent ?? "").not.toContain("unsupported_codec");
  });

  it("says so in German too", async () => {
    useUploadStatus.mockReturnValue({
      upload: anUpload({ status: "failed", reason: "decode_failed" }),
      unreachable: false,
    });
    await sendFile("de");

    expect(await screen.findByText(de.addOriginal.upload.reasons.decode_failed)).toBeTruthy();
    expect(screen.getByText(de.addOriginal.upload.watch.fallback)).toBeTruthy();
  });

  it("says when it cannot read the status, without pretending anything stopped", async () => {
    useUploadStatus.mockReturnValue({
      upload: anUpload({ status: "hashing" }),
      unreachable: true,
    });
    await sendFile();

    expect(await screen.findByText(words.watch.unreachable)).toBeTruthy();
  });
});

/**
 * Photo sets (issue #116, ADR 0053). The questions are a video's, and the answers have to be
 * truthful about photographs: what leaves the computer (hashes, never a file name), what the
 * customer reads while it runs, and that a picture this browser cannot read does not lose the set.
 */
describe("adding a photo set", () => {
  const photos = [
    new File([new Uint8Array([1, 2])], "beach-01.jpg", { type: "image/jpeg" }),
    new File([new Uint8Array([3, 4])], "beach-02.jpg", { type: "image/jpeg" }),
  ];

  function chooseSet(language: "en" | "de" = "en", { confirm = true } = {}) {
    const words = language === "en" ? en : de;
    fireEvent.click(screen.getByLabelText(words.addOriginal.kind.photos));
    fireEvent.change(screen.getByLabelText(words.addOriginal.photos.file.label), {
      target: { files: photos },
    });
    fireEvent.change(screen.getByLabelText(words.addOriginal.rightsHolder.label), {
      target: { value: "Sasha Meyer" },
    });
    if (confirm) fireEvent.click(screen.getByLabelText(words.addOriginal.rights.statement));
  }

  /**
   * A run over the chosen set: progress, then a document built by the real builder. `held` keeps
   * the run open, which is the only way to see the progress line — a mock that resolves at once
   * takes the screen straight to the answer.
   */
  function succeedWithGeneratedSet(
    skipped: { index: number; reason: string; type: string }[] = [],
    held?: Promise<void>,
  ) {
    fingerprintPhotos.mockImplementation(
      async (files: readonly File[], options: Record<string, unknown>) => {
        const onProgress = options.onProgress as
          | ((progress: { done: number; total: number; skipped: number }) => void)
          | undefined;
        onProgress?.({ done: 1, total: files.length, skipped: skipped.length });
        if (held !== undefined) await held;
        const hashes = Array.from({ length: ORIENTATIONS }, (_, index) =>
          index.toString(16).padStart(2, "0").repeat(32),
        );
        const document = buildPhotoFingerprint({
          images: files.map((file, index) => ({
            sha256: index.toString(16).padStart(2, "0").repeat(32),
            size: file.size,
            quality: 90,
            hashes,
          })),
          title: String(options.title ?? ""),
          creators: (options.creators as string[] | undefined) ?? [],
          client: { tool: "marceline-browser 0.2.0" },
        });
        return { document, skipped };
      },
    );
  }

  it("offers the choice and leaves a video selected", () => {
    renderScreen();

    const video = screen.getByLabelText(en.addOriginal.kind.video) as HTMLInputElement;
    expect(video.checked).toBe(true);
    expect(screen.getByLabelText(en.addOriginal.kind.photos)).toBeTruthy();
  });

  it("sends the hashes of the set and never a file name", async () => {
    succeedWithGeneratedSet();
    renderScreen();
    chooseSet();
    start();

    await waitFor(() => expect(send).toHaveBeenCalled());
    const [write] = posted();
    expect(write?.path).toBe("/app/api/fingerprints");
    const fingerprint = write?.body.fingerprint as Record<string, unknown>;
    expect(fingerprint.kind).toBe("photos");
    expect(fingerprint.image_count).toBe(2);
    expect(write?.body.rights_text_version).toBe(RIGHTS_TEXT_VERSION);
    // A photo collection has no time anywhere, and no file name leaves the computer.
    const body = JSON.stringify(write?.body);
    expect(body).not.toContain("beach-01.jpg");
    expect(body).not.toContain("timestamp_s");
    expect(body).not.toContain("duration_s");
  });

  it("counts the photographs while it is still working", async () => {
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    succeedWithGeneratedSet([{ index: 1, reason: "decode_failed", type: "image/heic" }], held);
    renderScreen();
    chooseSet();
    start();

    // The bar's detail line carries the count and the percentage in one node, so match inside it.
    const counted = en.addOriginal.photos.progress.imagesCount
      .replace("{{done}}", "1")
      .replace("{{total}}", "2");
    expect(await screen.findByText(new RegExp(counted))).toBeTruthy();
    // And says at once how many pictures it could not read, without naming one.
    expect(screen.getByText(/could not be read/)).toBeTruthy();
    expect(document.body.textContent ?? "").not.toContain("beach-01.jpg");
    release();
  });

  it("says after the fact what was left out of the set", async () => {
    succeedWithGeneratedSet([{ index: 1, reason: "decode_failed", type: "image/heic" }]);
    renderScreen();
    chooseSet();
    start();

    await waitFor(() => expect(send).toHaveBeenCalled());
    expect(await screen.findByText(/could not be read/)).toBeTruthy();
    expect(screen.getByText(/HEIC/)).toBeTruthy();
  });

  it("asks for the photographs, not for a video file, when the set is empty", async () => {
    renderScreen();
    fireEvent.click(screen.getByLabelText(en.addOriginal.kind.photos));
    start();

    expect(await screen.findByText(en.addOriginal.photos.problems.missingFiles)).toBeTruthy();
  });

  it("says the same things in German", async () => {
    succeedWithGeneratedSet();
    renderScreen("de");
    chooseSet("de");
    fireEvent.click(screen.getByRole("button", { name: de.addOriginal.start }));

    await waitFor(() => expect(send).toHaveBeenCalled());
    expect(screen.queryByText(en.addOriginal.photos.lead)).toBeNull();
  });
});
