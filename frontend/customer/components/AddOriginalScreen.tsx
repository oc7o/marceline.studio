"use client";
/**
 * "Add an original" (`/app/originals/new`, issue #29): the customer registers a video — or a photo
 * set (issue #116, ADR 0053) — without the command-line client. **The files never leave this
 * computer**, and no picture of any kind is ever shown, the customer's own included: a preview
 * would need an element `lib/privacy.test.ts` forbids, and "it is only their own file" is not a
 * distinction code can enforce once such an element exists.
 *
 * - The fingerprinting code (Worker, PDQ WASM, decoder glue) is imported only when the customer
 *   presses start, so neither the app nor this form pays for it — `lib/lazyFingerprinting.test.ts`
 *   fails the build on a static import of it anywhere.
 * - The browser digests and hashes in a Web Worker while the page seeks through the video, and the
 *   only thing sent is the hashes-only document (`@shared/lib/videoFingerprint`) plus the rights
 *   declaration, whose wording and version are fixed (`@shared/app/addOriginal`).
 * - Offered only where `GET /app/me` says `can_add_originals`. On production the upload is received
 *   first (202) and added shortly after (ADR 0028 write path); the page says so and the overview
 *   follows it.
 *
 * Factual words only: an original, fingerprints, copies. Nothing here calls anything a leak.
 */
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import {
  type FailureMessage,
  type FormProblem,
  type OriginalKind,
  RIGHTS_TEXT_VERSION,
  UPLOAD_DECLARATION_VERSION,
  castFrom,
  formProblems,
  formatFileSize,
  localFailure,
  problemKey,
  uploadFailure,
} from "@shared/app/addOriginal";
import { isAccepted } from "@shared/app/submissions";
import {
  type Upload,
  framesPercent,
  isAdded,
  reasonKey,
  saysDeleted,
  statusKey,
} from "@shared/app/uploadStatus";
import { estimateRemainingMs, percent, remainingMinutes } from "@shared/lib/fingerprintProgress";
// The format only: `asRequestBody` and the rest of the pipeline arrive with the fingerprinting
// client below, so this screen's first load carries no code that runs before the customer starts.
import {
  IMAGE_ACCEPT,
  MAX_UPLOAD_IMAGES,
  collectionTitle,
} from "@shared/lib/photoFingerprintFormat";
import { DEFAULT_FPS, MAX_UPLOAD_POSITIONS, stem } from "@shared/lib/videoFingerprintFormat";

import { type Resource, translator } from "../lib/translate";
import { useUploadStatus } from "../lib/useUploadStatus";
import { send } from "../lib/write";
import { FileDrop } from "./FileDrop";
import { Card } from "./parts";
import { Banner } from "./ui";

const MAX_HOURS = Math.floor(MAX_UPLOAD_POSITIONS / DEFAULT_FPS / 3600);

const BUTTON =
  "inline-flex items-center gap-2 rounded-sm border px-3 py-1.5 text-button transition-colors duration-120 ease-standard disabled:cursor-not-allowed disabled:opacity-45";
const NEUTRAL = `${BUTTON} border-border-control bg-surface-100 text-ink hover:bg-surface-300`;
const PRIMARY = `${BUTTON} border-brand bg-brand text-on-brand hover:bg-brand-hover`;

/** 201/200: the original itself; 202 (production): a submission being added. */
type AddOriginalResult = Parameters<typeof isAccepted>[0];

type Phase =
  | { kind: "form" }
  | {
      kind: "working";
      title: string;
      digest: number;
      frames: number;
      framesDone: number;
      framesTotal: number;
      /**
       * Whole minutes still to go, `0` for "less than a minute", `null` while the run has not said
       * enough to estimate. Measured on this device, because that is the only rate that means
       * anything (`docs/research/browser-fingerprinting-speed.md`).
       */
      etaMinutes: number | null;
    }
  // A photo set (ADR 0053): one bar, counted in photographs, plus what was skipped so far.
  | { kind: "photos"; title: string; done: number; total: number; skipped: number }
  | { kind: "uploading"; title: string }
  // Cloud fingerprinting (ADR 0044): the file itself is going up, chunk by chunk.
  | { kind: "sending"; title: string; sent: number; total: number }
  // The file is with us and the server is fingerprinting it: the screen polls (ADR 0044
  // amendment 2026-09-18) rather than leaving the customer with one "queued" line.
  | { kind: "watching"; title: string; upload: Upload }
  | { kind: "done"; result: AddOriginalResult; skipped?: number }
  | { kind: "failed"; failure: FailureMessage };

/** How the fingerprints are computed. Local hashing is the default and stays pre-selected. */
type Method = "local" | "upload";

export function AddOriginalScreen({
  language,
  words,
  canUploadMedia = false,
}: {
  language: string;
  words: Resource;
  canUploadMedia?: boolean;
}) {
  const t = translator(words, language);
  const ids = useId();
  const [kind, setKind] = useState<OriginalKind>("video");
  const [file, setFile] = useState<File | null>(null);
  const [photos, setPhotos] = useState<File[]>([]);
  const [title, setTitle] = useState("");
  const [rightsHolder, setRightsHolder] = useState("");
  const [others, setOthers] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [method, setMethod] = useState<Method>("local");
  // The upload declaration (ADR 0044 amendment 2026-09-19): two separate ticks, asked for only
  // where the file itself would be sent, and cleared whenever that choice changes — a tick must
  // always belong to the file and the method in front of the customer.
  const [solePerformer, setSolePerformer] = useState(false);
  const [article9Consent, setArticle9Consent] = useState(false);
  const [showProblems, setShowProblems] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: "form" });
  const controller = useRef<AbortController | null>(null);
  // One hook, always called, doing nothing until there is an upload to watch: the polling stops
  // by itself on a terminal state, while the tab is hidden and when this screen goes away.
  const watched = useUploadStatus(phase.kind === "watching" ? phase.upload.id : null);

  // Leaving the page stops the work: the Worker is terminated and nothing is sent.
  useEffect(() => {
    const abort = controller;
    return () => abort.current?.abort();
  }, []);

  const photoMode = kind === "photos";
  const sending = method === "upload" && canUploadMedia && !photoMode;
  const problems = formProblems({
    file: photoMode ? null : file,
    files: photoMode ? photos : [],
    title,
    rightsHolder,
    others,
    confirmed,
    sending,
    solePerformer,
    article9Consent,
  });

  const chooseFile = (chosen: File | null) => {
    setFile(chosen);
    setSolePerformer(false);
    setArticle9Consent(false);
    if (chosen && !title.trim()) setTitle(stem(chosen.name));
  };

  const choosePhotos = (chosen: File[]) => {
    setPhotos(chosen);
    if (chosen.length > 0 && !title.trim()) {
      setTitle(collectionTitle(chosen.map((photo) => photo.name)));
    }
  };

  const chooseKind = (next: OriginalKind) => {
    setKind(next);
    setShowProblems(false);
  };

  /** Changing the method or the file clears the declaration: it was made about the other one. */
  const chooseMethod = (next: Method) => {
    setMethod(next);
    setSolePerformer(false);
    setArticle9Consent(false);
    setShowProblems(false);
  };

  const start = async () => {
    if (problems.length > 0) {
      setShowProblems(true);
      return;
    }
    const abort = new AbortController();
    controller.current = abort;
    const cleanTitle = title.trim();
    if (photoMode) {
      await addPhotos(cleanTitle, abort);
      return;
    }
    if (file === null) {
      setShowProblems(true);
      return;
    }
    if (sending) {
      await sendFile(file, cleanTitle, abort);
      return;
    }
    setPhase({
      kind: "working",
      title: cleanTitle,
      digest: 0,
      frames: 0,
      framesDone: 0,
      framesTotal: 0,
      etaMinutes: null,
    });

    let client: typeof import("@shared/lib/fingerprintClient");
    let format: typeof import("@shared/lib/videoFingerprint");
    try {
      // Both on demand: the hasher, and the module that builds and validates the document it
      // returns. `videoFingerprint` is already in the client's own chunk, so the second import
      // costs no request — it only keeps the builder out of this screen's first load (issue #99).
      client = await import("@shared/lib/fingerprintClient");
      format = await import("@shared/lib/videoFingerprint");
    } catch (error: unknown) {
      setPhase({ kind: "failed", failure: localFailure(error) });
      return;
    }

    let shown = { digest: -1, frames: -1, framesTotal: -1, etaMinutes: -1 };
    let document: Awaited<ReturnType<typeof client.fingerprintFile>>;
    try {
      document = await client.fingerprintFile(file, {
        title: cleanTitle,
        creators: castFrom(rightsHolder, others),
        fps: DEFAULT_FPS,
        maxPositions: MAX_UPLOAD_POSITIONS,
        signal: abort.signal,
        onProgress: (progress) => {
          const remaining = estimateRemainingMs(progress);
          const next = {
            digest: percent(progress.fileSize > 0 ? progress.bytesDigested / progress.fileSize : 0),
            frames: percent(
              progress.framesTotal > 0 ? progress.framesHashed / progress.framesTotal : 0,
            ),
            framesTotal: progress.framesTotal,
            // -1 stands for "nothing to say yet", so it can be compared like the percentages.
            etaMinutes: remaining === null ? -1 : remainingMinutes(remaining),
          };
          // Re-render only when a whole percent or a whole minute changes, not on every frame.
          if (
            next.digest === shown.digest &&
            next.frames === shown.frames &&
            next.framesTotal === shown.framesTotal &&
            next.etaMinutes === shown.etaMinutes
          ) {
            return;
          }
          shown = next;
          setPhase({
            kind: "working",
            title: cleanTitle,
            digest: next.digest,
            frames: next.frames,
            framesDone: progress.framesHashed,
            framesTotal: progress.framesTotal,
            etaMinutes: next.etaMinutes < 0 ? null : next.etaMinutes,
          });
        },
      });
    } catch (error: unknown) {
      const kind = client.failureKind(error);
      if (kind !== null) {
        // The counters that decide whether assisted fingerprinting is worth building (#59):
        // the decoder's words and the container type, never the file name. Fire and forget.
        void send("/app/api/fingerprints/client-events", "POST", {
          kind,
          detail: (error instanceof Error ? error.message : String(error)).slice(0, 200),
          container: file.type.slice(0, 64),
          codec: "",
        }).catch(() => undefined);
      }
      setPhase({ kind: "failed", failure: localFailure(error) });
      return;
    }

    setPhase({ kind: "uploading", title: cleanTitle });
    try {
      const result = (await send("/app/api/fingerprints", "POST", {
        fingerprint: format.asRequestBody(document),
        rights_confirmed: true,
        rights_text_version: RIGHTS_TEXT_VERSION,
        language,
      })) as AddOriginalResult;
      setPhase({ kind: "done", result });
    } catch (error: unknown) {
      setPhase({ kind: "failed", failure: uploadFailure(error) });
    }
  };

  /**
   * A photo set (issue #116, ADR 0053): every photograph is hashed here, one at a time, and only
   * the hashes are sent. A picture this browser cannot read is counted and the set goes on; the
   * screen says how many were left out and why. No photograph is ever rendered — not the pirated
   * copy and not the customer's own.
   */
  const addPhotos = async (cleanTitle: string, abort: AbortController) => {
    setPhase({ kind: "photos", title: cleanTitle, done: 0, total: photos.length, skipped: 0 });
    let client: typeof import("@shared/lib/fingerprintClient");
    let format: typeof import("@shared/lib/photoFingerprint");
    try {
      client = await import("@shared/lib/fingerprintClient");
      format = await import("@shared/lib/photoFingerprint");
    } catch (error: unknown) {
      setPhase({ kind: "failed", failure: localFailure(error, "photos") });
      return;
    }

    let result: Awaited<ReturnType<typeof client.fingerprintPhotos>>;
    try {
      result = await client.fingerprintPhotos(photos, {
        title: cleanTitle,
        creators: castFrom(rightsHolder, others),
        maxImages: MAX_UPLOAD_IMAGES,
        signal: abort.signal,
        onProgress: (progress) => {
          setPhase({
            kind: "photos",
            title: cleanTitle,
            done: progress.done,
            total: progress.total,
            skipped: progress.skipped,
          });
        },
      });
    } catch (error: unknown) {
      const failureKind = client.failureKind(error);
      if (failureKind !== null) {
        // The same counters as a video (ADR 0019): the decoder's words and the MIME type of the
        // first file, never a file name.
        void send("/app/api/fingerprints/client-events", "POST", {
          kind: failureKind,
          detail: (error instanceof Error ? error.message : String(error)).slice(0, 200),
          container: (photos[0]?.type ?? "").slice(0, 64),
          codec: "",
        }).catch(() => undefined);
      }
      setPhase({ kind: "failed", failure: localFailure(error, "photos") });
      return;
    }

    setPhase({ kind: "uploading", title: cleanTitle });
    try {
      const answer = (await send("/app/api/fingerprints", "POST", {
        fingerprint: format.asPhotoRequestBody(result.document),
        rights_confirmed: true,
        rights_text_version: RIGHTS_TEXT_VERSION,
        language,
      })) as AddOriginalResult;
      setPhase({ kind: "done", result: answer, skipped: result.skipped.length });
    } catch (error: unknown) {
      setPhase({ kind: "failed", failure: uploadFailure(error) });
    }
  };

  /**
   * Cloud fingerprinting (ADR 0044): send the file instead of hashing it here. The module is
   * imported on demand, like the fingerprinting client, so a customer who never chooses this
   * never downloads it.
   */
  const sendFile = async (chosen: File, cleanTitle: string, abort: AbortController) => {
    setPhase({ kind: "sending", title: cleanTitle, sent: 0, total: chosen.size });
    try {
      const { uploadFile } = await import("../lib/uploadMedia");
      let shown = -1;
      const queued = await uploadFile(chosen, {
        title: cleanTitle,
        creators: castFrom(rightsHolder, others),
        fps: DEFAULT_FPS,
        language,
        rightsTextVersion: RIGHTS_TEXT_VERSION,
        uploadDeclarationVersion: UPLOAD_DECLARATION_VERSION,
        signal: abort.signal,
        onProgress: ({ sent, total }) => {
          const done = percent(total > 0 ? sent / total : 0);
          if (done === shown) return;
          shown = done;
          setPhase({ kind: "sending", title: cleanTitle, sent, total });
        },
      });
      setPhase({ kind: "watching", title: cleanTitle, upload: queued });
    } catch (error: unknown) {
      if (error instanceof Error && error.name === "UploadCancelled") {
        setPhase({ kind: "failed", failure: { key: "addOriginal.errors.local.cancelled" } });
        return;
      }
      setPhase({ kind: "failed", failure: uploadFailure(error) });
    }
  };

  const startOver = () => {
    setFile(null);
    setPhotos([]);
    setTitle("");
    setRightsHolder("");
    setOthers("");
    setConfirmed(false);
    setSolePerformer(false);
    setArticle9Consent(false);
    setShowProblems(false);
    setPhase({ kind: "form" });
  };

  if (phase.kind === "sending") {
    const done = percent(phase.total > 0 ? phase.sent / phase.total : 0);
    return (
      <Card labelledBy={`${ids}-sending`}>
        <h2 id={`${ids}-sending`} className="font-display text-display-s break-words text-ink">
          {t("addOriginal.upload.progress.heading", { title: phase.title })}
        </h2>
        <p className="mt-1 text-body-s text-ink-muted">
          {t("addOriginal.upload.progress.keepOpen")}
        </p>
        <div className="mt-3 flex flex-col gap-3">
          <ProgressBar
            label={t("addOriginal.upload.progress.sending")}
            value={done}
            detail={`${formatFileSize(phase.sent, language)} / ${formatFileSize(phase.total, language)}`}
          />
          <div>
            <button type="button" className={NEUTRAL} onClick={() => controller.current?.abort()}>
              {t("addOriginal.cancel")}
            </button>
          </div>
        </div>
      </Card>
    );
  }

  if (phase.kind === "watching") {
    // The answer we have, which is the queued job until the first poll comes back.
    const upload = watched.upload ?? phase.upload;
    const deleted = saysDeleted(upload) ? (
      <p className="mt-1 text-body-s text-ink-muted">{t("addOriginal.upload.watch.deleted")}</p>
    ) : null;

    if (isAdded(upload)) {
      return (
        <Card>
          <p role="status" className="text-body text-ink">
            {t("addOriginal.upload.watch.added", { title: phase.title })}
          </p>
          {deleted}
          <div className="mt-3 flex flex-wrap items-center gap-3 text-body-s">
            <a
              href={`/app/originals/${upload.original_id}`}
              className="rounded-sm text-button text-brand-ink underline"
            >
              {t("addOriginal.done.open")}
            </a>
            <button type="button" className={NEUTRAL} onClick={startOver}>
              {t("addOriginal.done.another")}
            </button>
          </div>
        </Card>
      );
    }

    if (upload.reason) {
      return (
        <div className="flex flex-col gap-4">
          <Banner tone="critical" role="alert">
            <p className="font-semibold">
              {t("addOriginal.upload.watch.failed", { title: phase.title })}
            </p>
            <p className="mt-1">{t(reasonKey(upload.reason))}</p>
            <p className="mt-1">{t("addOriginal.upload.watch.fallback")}</p>
          </Banner>
          <Card>
            {deleted}
            <div className="mt-3 flex flex-wrap items-center gap-3 text-body-s">
              <a href="/app" className="rounded-sm text-button text-brand-ink underline">
                {t("addOriginal.done.overview")}
              </a>
              <button type="button" className={NEUTRAL} onClick={startOver}>
                {t("addOriginal.done.another")}
              </button>
            </div>
          </Card>
        </div>
      );
    }

    const frames = framesPercent(upload);
    return (
      <Card labelledBy={`${ids}-watch`}>
        <h2 id={`${ids}-watch`} className="font-display text-display-s break-words text-ink">
          {t("addOriginal.upload.watch.heading", { title: phase.title })}
        </h2>
        <p className="mt-1 text-body-s text-ink-muted">{t("addOriginal.upload.watch.leave")}</p>
        <div className="mt-3 flex flex-col gap-3">
          <ProgressBar
            label={t("addOriginal.upload.watch.hashing")}
            value={frames}
            detail={
              frames === null || upload.frames_total === null
                ? t("addOriginal.upload.watch.framesWaiting")
                : t("addOriginal.upload.watch.frames", {
                    done: upload.frames_done,
                    total: upload.frames_total,
                  })
            }
          />
          <p role="status" className="text-body-s text-ink">
            {t(statusKey(upload))}
          </p>
          {watched.unreachable ? (
            <p className="text-body-s text-ink-muted">
              {t("addOriginal.upload.watch.unreachable")}
            </p>
          ) : null}
          <div className="flex flex-wrap items-center gap-3 text-body-s">
            <a href="/app" className="rounded-sm text-button text-brand-ink underline">
              {t("addOriginal.done.overview")}
            </a>
          </div>
        </div>
      </Card>
    );
  }

  if (phase.kind === "photos") {
    const done = percent(phase.total > 0 ? phase.done / phase.total : 0);
    return (
      <Card labelledBy={`${ids}-photos`}>
        <h2 id={`${ids}-photos`} className="font-display text-display-s break-words text-ink">
          {t("addOriginal.progress.heading", { title: phase.title })}
        </h2>
        <p className="mt-1 text-body-s text-ink-muted">{t("addOriginal.photos.progress.keepOpen")}</p>
        <div className="mt-3 flex flex-col gap-3">
          <ProgressBar
            label={t("addOriginal.photos.progress.images")}
            value={done}
            detail={t("addOriginal.photos.progress.imagesCount", {
              done: phase.done,
              total: phase.total,
            })}
          />
          {phase.skipped > 0 ? (
            <p className="text-body-s text-ink-muted">
              {t("addOriginal.photos.skipped", { count: phase.skipped })}
            </p>
          ) : null}
          <div>
            <button type="button" className={NEUTRAL} onClick={() => controller.current?.abort()}>
              {t("addOriginal.cancel")}
            </button>
          </div>
        </div>
      </Card>
    );
  }

  if (phase.kind === "working" || phase.kind === "uploading") {
    return (
      <Card labelledBy={`${ids}-progress`}>
        <h2 id={`${ids}-progress`} className="font-display text-display-s break-words text-ink">
          {t("addOriginal.progress.heading", { title: phase.title })}
        </h2>
        <p className="mt-1 text-body-s text-ink-muted">{t("addOriginal.progress.keepOpen")}</p>
        {phase.kind === "working" ? (
          <div className="mt-3 flex flex-col gap-3">
            <ProgressBar label={t("addOriginal.progress.digest")} value={phase.digest} />
            <ProgressBar
              label={t("addOriginal.progress.frames")}
              value={phase.frames}
              detail={
                phase.framesTotal > 0
                  ? t("addOriginal.progress.framesCount", {
                      done: phase.framesDone,
                      total: phase.framesTotal,
                    })
                  : t("addOriginal.progress.framesWaiting")
              }
            />
            {phase.etaMinutes !== null ? (
              // A live estimate, measured on this device: the wait ranges from under a minute to
              // the better part of an hour depending on the file and the machine
              // (docs/research/browser-fingerprinting-speed.md), so a bar alone says nothing about
              // whether to stay. `aria-live="polite"` because it changes while the screen is read.
              <p role="status" aria-live="polite" className="text-body-s text-ink-muted">
                {phase.etaMinutes === 0
                  ? t("addOriginal.progress.etaSoon")
                  : t("addOriginal.progress.eta", { count: phase.etaMinutes })}
              </p>
            ) : null}
            <div>
              <button
                type="button"
                className={NEUTRAL}
                onClick={() => controller.current?.abort()}
              >
                {t("addOriginal.cancel")}
              </button>
            </div>
          </div>
        ) : (
          <p role="status" className="mt-3 text-body-s text-ink">
            {t("addOriginal.progress.uploading")}
          </p>
        )}
      </Card>
    );
  }

  if (phase.kind === "done") {
    if (isAccepted(phase.result)) {
      // Production (ADR 0028 write path): received now, added shortly; the overview follows it.
      return (
        <Card>
          <p role="status" className="text-body text-ink">
            {t("addOriginal.done.received", { title: phase.result.submission.title })}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-3 text-body-s">
            <a href="/app" className="rounded-sm text-button text-brand-ink underline">
              {t("addOriginal.done.overview")}
            </a>
            <button type="button" className={NEUTRAL} onClick={startOver}>
              {t("addOriginal.done.another")}
            </button>
          </div>
        </Card>
      );
    }
    const { original, created } = phase.result;
    return (
      <Card>
        <p role="status" className="text-body text-ink">
          {created
            ? t("addOriginal.done.created", { title: original.title })
            : t("addOriginal.done.existing", { title: original.title })}
        </p>
        {phase.skipped ? (
          <p className="mt-1 text-body-s text-ink-muted">
            {t("addOriginal.photos.skipped", { count: phase.skipped })}
          </p>
        ) : null}
        <div className="mt-3 flex flex-wrap items-center gap-3 text-body-s">
          <a
            href={`/app/originals/${original.id}`}
            className="rounded-sm text-button text-brand-ink underline"
          >
            {t("addOriginal.done.open")}
          </a>
          <button type="button" className={NEUTRAL} onClick={startOver}>
            {t("addOriginal.done.another")}
          </button>
        </div>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="max-w-[68ch] text-body text-ink-muted">
        {t(photoMode ? "addOriginal.photos.lead" : "addOriginal.lead")}
      </p>
      {phase.kind === "failed" ? (
        <Banner tone="critical" role="alert">
          <p className="font-semibold">{t("addOriginal.errors.heading")}</p>
          <p className="mt-1">{t(phase.failure.key, { count: phase.failure.count })}</p>
        </Banner>
      ) : null}
      <form
        className="flex flex-col gap-4 rounded-md border border-hairline bg-surface-100 p-4 shadow-sm"
        onSubmit={(event) => {
          event.preventDefault();
          void start();
        }}
        noValidate
      >
        {/*
          The field's own `<label htmlFor>` is the input's accessible name and stays; the drop
          target repeats it in view, so it is not shown twice. Still no preview of any kind — the
          file is read by the Worker and never rendered (issue #53).
        */}
        <fieldset className="rounded-md border border-hairline bg-surface-inset p-3">
          <legend className="px-1 text-label text-ink">{t("addOriginal.kind.legend")}</legend>
          <div className="flex flex-col gap-2">
            <KindChoice
              name={`${ids}-kind`}
              value="video"
              checked={kind === "video"}
              label={t("addOriginal.kind.video")}
              onChoose={chooseKind}
            />
            <KindChoice
              name={`${ids}-kind`}
              value="photos"
              checked={photoMode}
              label={t("addOriginal.kind.photos")}
              onChoose={chooseKind}
            />
          </div>
        </fieldset>
        <div className="flex flex-col gap-1">
          <label htmlFor={`${ids}-file`} className="sr-only">
            {t(photoMode ? "addOriginal.photos.file.label" : "addOriginal.file.label")}
          </label>
          {photoMode ? (
            <FileDrop
              id={`${ids}-file`}
              multiple
              accept={IMAGE_ACCEPT}
              label={t("addOriginal.photos.file.label")}
              hint={t("addOriginal.photos.file.hint", { count: MAX_UPLOAD_IMAGES })}
              chosen={
                photos.length > 0
                  ? t("addOriginal.photos.file.chosen", {
                      count: photos.length,
                      size: formatFileSize(
                        photos.reduce((total, photo) => total + photo.size, 0),
                        language,
                      ),
                    })
                  : undefined
              }
              onChoose={choosePhotos}
            />
          ) : (
            <FileDrop
              id={`${ids}-file`}
              accept="video/mp4,video/quicktime,video/*"
              label={t("addOriginal.file.label")}
              hint={t("addOriginal.file.hint", { hours: MAX_HOURS })}
              chosen={
                file
                  ? t("addOriginal.file.chosen", {
                      name: file.name,
                      size: formatFileSize(file.size, language),
                    })
                  : undefined
              }
              onChoose={chooseFile}
            />
          )}
        </div>
        <FormField
          id={`${ids}-title`}
          label={t("addOriginal.title.label")}
          hint={t("addOriginal.title.hint")}
        >
          <input
            id={`${ids}-title`}
            type="text"
            value={title}
            maxLength={255}
            onChange={(event) => setTitle(event.target.value)}
            aria-describedby={`${ids}-title-hint`}
            className="w-full rounded-sm border border-border-control bg-surface-100 px-2 py-1.5 text-body-s text-ink"
          />
        </FormField>
        <FormField
          id={`${ids}-holder`}
          label={t("addOriginal.rightsHolder.label")}
          hint={t(
            photoMode ? "addOriginal.photos.rightsHolder.hint" : "addOriginal.rightsHolder.hint",
          )}
        >
          <input
            id={`${ids}-holder`}
            type="text"
            value={rightsHolder}
            maxLength={255}
            autoComplete="off"
            onChange={(event) => setRightsHolder(event.target.value)}
            aria-describedby={`${ids}-holder-hint`}
            className="w-full rounded-sm border border-border-control bg-surface-100 px-2 py-1.5 text-body-s text-ink"
          />
        </FormField>
        <FormField
          id={`${ids}-others`}
          label={t("addOriginal.others.label")}
          hint={t("addOriginal.others.hint")}
        >
          <textarea
            id={`${ids}-others`}
            value={others}
            rows={3}
            autoComplete="off"
            onChange={(event) => setOthers(event.target.value)}
            aria-describedby={`${ids}-others-hint`}
            className="w-full rounded-sm border border-border-control bg-surface-100 px-2 py-1.5 text-body-s text-ink"
          />
        </FormField>
        <fieldset className="rounded-md border border-hairline bg-surface-inset p-3">
          <legend className="px-1 text-label text-ink">{t("addOriginal.rights.legend")}</legend>
          <label className="flex items-start gap-2 text-body-s text-ink">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              className="mt-1"
            />
            <span>{t("addOriginal.rights.statement")}</span>
          </label>
        </fieldset>
        {canUploadMedia && !photoMode ? (
          <fieldset className="rounded-md border border-hairline bg-surface-inset p-3">
            <legend className="px-1 text-label text-ink">{t("addOriginal.method.legend")}</legend>
            <div className="flex flex-col gap-2">
              <MethodChoice
                name={`${ids}-method`}
                value="local"
                checked={method === "local"}
                label={t("addOriginal.method.local")}
                hint={t("addOriginal.method.localHint")}
                onChoose={chooseMethod}
              />
              <MethodChoice
                name={`${ids}-method`}
                value="upload"
                checked={method === "upload"}
                label={t("addOriginal.method.upload")}
                hint={t("addOriginal.method.uploadHint")}
                onChoose={chooseMethod}
              />
            </div>
          </fieldset>
        ) : null}
        {sending ? (
          <fieldset className="rounded-md border border-hairline bg-surface-inset p-3">
            <legend className="px-1 text-label text-ink">
              {t("addOriginal.upload.declaration.legend")}
            </legend>
            <div className="flex flex-col gap-2">
              <label className="flex items-start gap-2 text-body-s text-ink">
                <input
                  type="checkbox"
                  checked={solePerformer}
                  onChange={(event) => setSolePerformer(event.target.checked)}
                  className="mt-1"
                />
                <span>{t("addOriginal.upload.declaration.solePerformer")}</span>
              </label>
              <label className="flex items-start gap-2 text-body-s text-ink">
                <input
                  type="checkbox"
                  checked={article9Consent}
                  onChange={(event) => setArticle9Consent(event.target.checked)}
                  className="mt-1"
                />
                <span>{t("addOriginal.upload.declaration.article9Consent")}</span>
              </label>
              <p className="text-body-s text-ink-muted">
                {t("addOriginal.upload.declaration.others")}
              </p>
            </div>
          </fieldset>
        ) : null}
        {showProblems && problems.length > 0 ? (
          <Problems problems={problems} label={t("addOriginal.problems.heading")} kind={kind} t={t} />
        ) : null}
        <div>
          <button type="submit" className={PRIMARY}>
            {sending ? t("addOriginal.upload.start") : t("addOriginal.start")}
          </button>
        </div>
      </form>
    </div>
  );
}

function FormField({
  id,
  label,
  hint,
  children,
}: {
  id: string;
  label: string;
  hint: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-label text-ink">
        {label}
      </label>
      {children}
      <p id={`${id}-hint`} className="text-caption text-ink-subtle">
        {hint}
      </p>
    </div>
  );
}

function MethodChoice({
  name,
  value,
  checked,
  label,
  hint,
  onChoose,
}: {
  name: string;
  value: Method;
  checked: boolean;
  label: string;
  hint: string;
  onChoose: (method: Method) => void;
}) {
  return (
    <label className="flex items-start gap-2 text-body-s text-ink">
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        onChange={() => onChoose(value)}
        className="mt-1"
      />
      <span>
        <span className="font-medium">{label}</span>
        <span className="block text-caption text-ink-subtle">{hint}</span>
      </span>
    </label>
  );
}

/** Video or photo set. A radio group, because it changes what the picker takes. */
function KindChoice({
  name,
  value,
  checked,
  label,
  onChoose,
}: {
  name: string;
  value: OriginalKind;
  checked: boolean;
  label: string;
  onChoose: (kind: OriginalKind) => void;
}) {
  return (
    <label className="flex items-start gap-2 text-body-s text-ink">
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        onChange={() => onChoose(value)}
        className="mt-1"
      />
      <span className="font-medium">{label}</span>
    </label>
  );
}

function Problems({
  problems,
  label,
  kind,
  t,
}: {
  problems: readonly FormProblem[];
  label: string;
  kind: OriginalKind;
  t: (key: string) => string;
}) {
  return (
    <Banner tone="caution" role="alert">
      <p className="font-medium">{label}</p>
      <ul className="mt-1 list-disc pl-5">
        {problems.map((problem) => (
          <li key={problem}>{t(problemKey(problem, kind))}</li>
        ))}
      </ul>
    </Banner>
  );
}

/**
 * `value === null` is "we do not know how much there is yet" — a cloud upload has no denominator
 * until the file has been probed (ADR 0044 amendment). The bar then says so instead of standing
 * at 0 %, and carries no `aria-valuenow`, which is exactly what an indeterminate progressbar is.
 */
function ProgressBar({
  label,
  value,
  detail,
}: {
  label: string;
  value: number | null;
  detail?: string;
}) {
  const known = value !== null;
  return (
    <div>
      <div className="flex flex-wrap justify-between gap-2 text-body-s text-ink">
        <span>{label}</span>
        <span className="font-mono text-mono-s text-ink-muted">
          {known ? (detail ? `${detail} · ${value}%` : `${value}%`) : (detail ?? "")}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        {...(known ? { "aria-valuenow": value } : {})}
        className="mt-1 h-2 w-full overflow-hidden rounded-full bg-surface-300"
      >
        <div
          className={`h-full bg-accent transition-[width] ${known ? "" : "animate-pulse"}`}
          style={{ width: known ? `${value}%` : "33%" }}
        />
      </div>
    </div>
  );
}
