/**
 * "Add an original" (`/app/originals/new`, issue #29): the customer registers a video — or a photo
 * set (issue #116, ADR 0053) — without the command-line client. **The files never leave this
 * computer**, and no picture of any kind is ever shown, the customer's own included.
 *
 * - This screen is a lazily loaded route; the fingerprinting code (Worker, PDQ WASM, decoder glue)
 *   is imported only when the customer presses start, so neither the app nor this form pays for it.
 * - The browser digests and hashes in a Web Worker while the page seeks through the video, and
 *   the only thing sent is the hashes-only document (`lib/videoFingerprint.ts`) plus the rights
 *   declaration, whose wording and version are fixed (`addOriginal.ts`).
 * - Offered only where `GET /app/me` says `can_add_originals`. On production the upload is
 *   received first (202) and added shortly after (ADR 0028 write path); the page says so and the
 *   overview follows it (`submissions.ts`).
 *
 * Factual words only: an original, fingerprints, copies. Nothing here calls anything a leak.
 */
import { Link } from "@tanstack/react-router";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../components/ui";
import { useLanguage } from "../i18n";
import { estimateRemainingMs, percent, remainingMinutes } from "../lib/fingerprintProgress";
import {
  IMAGE_ACCEPT,
  MAX_UPLOAD_IMAGES,
  collectionTitle,
} from "../lib/photoFingerprintFormat";
import { DEFAULT_FPS, MAX_UPLOAD_POSITIONS, asRequestBody, stem } from "../lib/videoFingerprint";
import {
  type FailureMessage,
  type FormProblem,
  type OriginalKind,
  RIGHTS_TEXT_VERSION,
  castFrom,
  formProblems,
  formatFileSize,
  localFailure,
  problemKey,
  uploadFailure,
} from "./addOriginal";
import { Card, LoadError, Loading } from "./parts";
import { type AddOriginalResult, useAddOriginal, useCustomerAccount, useRecordClientEvent } from "./queries";
import { isAccepted } from "./submissions";

const MAX_HOURS = Math.floor(MAX_UPLOAD_POSITIONS / DEFAULT_FPS / 3600);

export function AddOriginalScreen() {
  const { t } = useTranslation();
  const account = useCustomerAccount();

  if (account.isPending) return <Loading />;
  if (account.isError) return <LoadError onRetry={() => void account.refetch()} />;
  return (
    <div className="flex flex-col gap-4">
      <Link to="/app" className="text-sm text-sky-800 underline">
        {t("addOriginal.back")}
      </Link>
      <h1 className="text-xl font-bold">{t("addOriginal.heading")}</h1>
      {account.data.organisation === null ? (
        <p role="status" className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          {t("home.noOrganisation")}
        </p>
      ) : !account.data.can_add_originals ? (
        <p role="status" className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-700">
          {t("addOriginal.unavailable")}
        </p>
      ) : (
        <AddOriginalFlow />
      )}
    </div>
  );
}

type Phase =
  | { kind: "form" }
  | {
      kind: "working";
      title: string;
      digest: number;
      frames: number;
      framesDone: number;
      framesTotal: number;
      /** Whole minutes left, 0 for "under a minute", null while the run has not said enough. */
      etaMinutes: number | null;
    }
  // A photo set (issue #116, ADR 0053): one bar, counted in photographs, plus what was skipped.
  | { kind: "photos"; title: string; done: number; total: number; skipped: number }
  | { kind: "uploading"; title: string }
  | { kind: "done"; result: AddOriginalResult; skipped?: number }
  | { kind: "failed"; failure: FailureMessage };

function AddOriginalFlow() {
  const { t } = useTranslation();
  const [language] = useLanguage();
  const ids = useId();
  const [kind, setKind] = useState<OriginalKind>("video");
  const [file, setFile] = useState<File | null>(null);
  const [photos, setPhotos] = useState<File[]>([]);
  const [title, setTitle] = useState("");
  const [rightsHolder, setRightsHolder] = useState("");
  const [others, setOthers] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [showProblems, setShowProblems] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: "form" });
  const controller = useRef<AbortController | null>(null);
  const add = useAddOriginal();
  const recordFailure = useRecordClientEvent();

  // Leaving the page stops the work: the Worker is terminated and nothing is sent.
  useEffect(() => () => controller.current?.abort(), []);

  const photoMode = kind === "photos";
  const problems = formProblems({
    file: photoMode ? null : file,
    files: photoMode ? photos : [],
    title,
    rightsHolder,
    others,
    confirmed,
  });

  const chooseFile = (chosen: File | null) => {
    setFile(chosen);
    if (chosen && !title.trim()) setTitle(stem(chosen.name));
  };

  const choosePhotos = (chosen: File[]) => {
    setPhotos(chosen);
    if (chosen.length > 0 && !title.trim()) {
      setTitle(collectionTitle(chosen.map((photo) => photo.name)));
    }
  };

  const start = async () => {
    if (problems.length > 0 || (photoMode ? photos.length === 0 : file === null)) {
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
    if (file === null) return;
    setPhase({
      kind: "working",
      title: cleanTitle,
      digest: 0,
      frames: 0,
      framesDone: 0,
      framesTotal: 0,
      etaMinutes: null,
    });

    let client: typeof import("../lib/fingerprintClient");
    try {
      client = await import("../lib/fingerprintClient");
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
            frames: percent(progress.framesTotal > 0 ? progress.framesHashed / progress.framesTotal : 0),
            framesTotal: progress.framesTotal,
            // -1 stands for "nothing to say yet", so it compares like the percentages.
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
      const failed = client.failureKind(error);
      if (failed !== null) {
        // The counters that decide whether assisted fingerprinting is worth building (#59):
        // the decoder's words and the container type, never the file name.
        recordFailure.mutate({
          kind: failed,
          detail: (error instanceof Error ? error.message : String(error)).slice(0, 200),
          container: file.type.slice(0, 64),
          codec: "",
        });
      }
      setPhase({ kind: "failed", failure: localFailure(error) });
      return;
    }

    setPhase({ kind: "uploading", title: cleanTitle });
    try {
      const result = await add.mutateAsync({
        fingerprint: asRequestBody(document),
        rights_confirmed: true,
        rights_text_version: RIGHTS_TEXT_VERSION,
        language,
      });
      setPhase({ kind: "done", result });
    } catch (error: unknown) {
      setPhase({ kind: "failed", failure: uploadFailure(error) });
    }
  };

  /**
   * A photo set (issue #116, ADR 0053): every photograph is hashed here, one at a time, and only
   * the hashes are sent. A picture this browser cannot read is counted and the set goes on. No
   * photograph is ever rendered — not the pirated copy, and not the customer's own.
   */
  const addPhotos = async (cleanTitle: string, abort: AbortController) => {
    setPhase({ kind: "photos", title: cleanTitle, done: 0, total: photos.length, skipped: 0 });
    let client: typeof import("../lib/fingerprintClient");
    let format: typeof import("../lib/photoFingerprint");
    try {
      client = await import("../lib/fingerprintClient");
      format = await import("../lib/photoFingerprint");
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
      const failed = client.failureKind(error);
      if (failed !== null) {
        recordFailure.mutate({
          kind: failed,
          detail: (error instanceof Error ? error.message : String(error)).slice(0, 200),
          container: (photos[0]?.type ?? "").slice(0, 64),
          codec: "",
        });
      }
      setPhase({ kind: "failed", failure: localFailure(error, "photos") });
      return;
    }

    setPhase({ kind: "uploading", title: cleanTitle });
    try {
      const answer = await add.mutateAsync({
        fingerprint: format.asPhotoRequestBody(result.document),
        rights_confirmed: true,
        rights_text_version: RIGHTS_TEXT_VERSION,
        language,
      });
      setPhase({ kind: "done", result: answer, skipped: result.skipped.length });
    } catch (error: unknown) {
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
    setShowProblems(false);
    setPhase({ kind: "form" });
  };

  if (phase.kind === "photos") {
    return (
      <Card labelledBy={`${ids}-photos`}>
        <h2 id={`${ids}-photos`} className="font-semibold">
          {t("addOriginal.progress.heading", { title: phase.title })}
        </h2>
        <p className="mt-1 text-sm text-slate-600">{t("addOriginal.photos.progress.keepOpen")}</p>
        <div className="mt-3 flex flex-col gap-3">
          <ProgressBar
            label={t("addOriginal.photos.progress.images")}
            value={percent(phase.total > 0 ? phase.done / phase.total : 0)}
            detail={t("addOriginal.photos.progress.imagesCount", {
              done: phase.done,
              total: phase.total,
            })}
          />
          {phase.skipped > 0 ? (
            <p className="text-sm text-slate-600">
              {t("addOriginal.photos.skipped", { count: phase.skipped })}
            </p>
          ) : null}
          <div>
            <Button onClick={() => controller.current?.abort()}>{t("addOriginal.cancel")}</Button>
          </div>
        </div>
      </Card>
    );
  }

  if (phase.kind === "working" || phase.kind === "uploading") {
    return (
      <Card labelledBy={`${ids}-progress`}>
        <h2 id={`${ids}-progress`} className="font-semibold">
          {t("addOriginal.progress.heading", { title: phase.title })}
        </h2>
        <p className="mt-1 text-sm text-slate-600">{t("addOriginal.progress.keepOpen")}</p>
        {phase.kind === "working" ? (
          <div className="mt-3 flex flex-col gap-3">
            <ProgressBar label={t("addOriginal.progress.digest")} value={phase.digest} />
            <ProgressBar
              label={t("addOriginal.progress.frames")}
              value={phase.frames}
              detail={
                phase.framesTotal > 0
                  ? t("addOriginal.progress.framesCount", { done: phase.framesDone, total: phase.framesTotal })
                  : t("addOriginal.progress.framesWaiting")
              }
            />
            {phase.etaMinutes !== null ? (
              <p role="status" aria-live="polite" className="text-sm text-slate-600">
                {phase.etaMinutes === 0
                  ? t("addOriginal.progress.etaSoon")
                  : t("addOriginal.progress.eta", { count: phase.etaMinutes })}
              </p>
            ) : null}
            <div>
              <Button onClick={() => controller.current?.abort()}>{t("addOriginal.cancel")}</Button>
            </div>
          </div>
        ) : (
          <p role="status" className="mt-3 text-sm text-slate-700">
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
          <p role="status" className="text-sm text-slate-800">
            {t("addOriginal.done.received", { title: phase.result.submission.title })}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
            <Link to="/app" className="font-medium text-sky-800 underline">
              {t("addOriginal.done.overview")}
            </Link>
            <Button onClick={startOver}>{t("addOriginal.done.another")}</Button>
          </div>
        </Card>
      );
    }
    const { original, created } = phase.result;
    return (
      <Card>
        <p role="status" className="text-sm text-slate-800">
          {created
            ? t("addOriginal.done.created", { title: original.title })
            : t("addOriginal.done.existing", { title: original.title })}
        </p>
        {phase.skipped ? (
          <p className="mt-1 text-sm text-slate-600">
            {t("addOriginal.photos.skipped", { count: phase.skipped })}
          </p>
        ) : null}
        <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
          <Link
            to="/app/originals/$originalId"
            params={{ originalId: original.id }}
            className="font-medium text-sky-800 underline"
          >
            {t("addOriginal.done.open")}
          </Link>
          <Button onClick={startOver}>{t("addOriginal.done.another")}</Button>
        </div>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-slate-600">
        {t(photoMode ? "addOriginal.photos.lead" : "addOriginal.lead")}
      </p>
      {phase.kind === "failed" ? (
        <div role="alert" className="rounded border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
          <p className="font-semibold">{t("addOriginal.errors.heading")}</p>
          <p className="mt-1">{t(phase.failure.key, { count: phase.failure.count })}</p>
        </div>
      ) : null}
      <form
        className="flex flex-col gap-4 rounded-lg border border-slate-200 bg-white p-4"
        onSubmit={(event) => {
          event.preventDefault();
          void start();
        }}
        noValidate
      >
        <fieldset className="rounded border border-slate-200 p-3">
          <legend className="px-1 text-sm font-medium">{t("addOriginal.kind.legend")}</legend>
          <div className="flex flex-col gap-2 text-sm">
            <label className="flex items-start gap-2">
              <input
                type="radio"
                name={`${ids}-kind`}
                checked={!photoMode}
                onChange={() => {
                  setKind("video");
                  setShowProblems(false);
                }}
                className="mt-1"
              />
              <span>{t("addOriginal.kind.video")}</span>
            </label>
            <label className="flex items-start gap-2">
              <input
                type="radio"
                name={`${ids}-kind`}
                checked={photoMode}
                onChange={() => {
                  setKind("photos");
                  setShowProblems(false);
                }}
                className="mt-1"
              />
              <span>{t("addOriginal.kind.photos")}</span>
            </label>
          </div>
        </fieldset>
        {photoMode ? (
          <FormField
            id={`${ids}-file`}
            label={t("addOriginal.photos.file.label")}
            hint={t("addOriginal.photos.file.hint", { count: MAX_UPLOAD_IMAGES })}
          >
            <input
              id={`${ids}-file`}
              type="file"
              multiple
              accept={IMAGE_ACCEPT}
              className="text-sm"
              aria-describedby={`${ids}-file-hint`}
              onChange={(event) => choosePhotos([...(event.target.files ?? [])])}
            />
            {photos.length > 0 ? (
              <p className="mt-1 text-xs text-slate-600">
                {t("addOriginal.photos.file.chosen", {
                  count: photos.length,
                  size: formatFileSize(
                    photos.reduce((total, photo) => total + photo.size, 0),
                    language,
                  ),
                })}
              </p>
            ) : null}
          </FormField>
        ) : (
          <FormField id={`${ids}-file`} label={t("addOriginal.file.label")} hint={t("addOriginal.file.hint", { hours: MAX_HOURS })}>
            <input
              id={`${ids}-file`}
              type="file"
              accept="video/mp4,video/quicktime,video/*"
              className="text-sm"
              aria-describedby={`${ids}-file-hint`}
              onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
            />
            {file ? (
              <p className="mt-1 text-xs break-all text-slate-600">
                {t("addOriginal.file.chosen", { name: file.name, size: formatFileSize(file.size, language) })}
              </p>
            ) : null}
          </FormField>
        )}
        <FormField id={`${ids}-title`} label={t("addOriginal.title.label")} hint={t("addOriginal.title.hint")}>
          <input
            id={`${ids}-title`}
            type="text"
            value={title}
            maxLength={255}
            onChange={(event) => setTitle(event.target.value)}
            aria-describedby={`${ids}-title-hint`}
            className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          />
        </FormField>
        <FormField
          id={`${ids}-holder`}
          label={t("addOriginal.rightsHolder.label")}
          hint={t("addOriginal.rightsHolder.hint")}
        >
          <input
            id={`${ids}-holder`}
            type="text"
            value={rightsHolder}
            maxLength={255}
            autoComplete="off"
            onChange={(event) => setRightsHolder(event.target.value)}
            aria-describedby={`${ids}-holder-hint`}
            className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          />
        </FormField>
        <FormField id={`${ids}-others`} label={t("addOriginal.others.label")} hint={t("addOriginal.others.hint")}>
          <textarea
            id={`${ids}-others`}
            value={others}
            rows={3}
            autoComplete="off"
            onChange={(event) => setOthers(event.target.value)}
            aria-describedby={`${ids}-others-hint`}
            className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
          />
        </FormField>
        <fieldset className="rounded border border-slate-200 p-3">
          <legend className="px-1 text-sm font-medium">{t("addOriginal.rights.legend")}</legend>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              className="mt-1"
            />
            <span>{t("addOriginal.rights.statement")}</span>
          </label>
        </fieldset>
        {showProblems && problems.length > 0 ? (
          <Problems problems={problems} kind={kind} />
        ) : null}
        <div>
          <Button type="submit" tone="primary">
            {t("addOriginal.start")}
          </Button>
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
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {children}
      <p id={`${id}-hint`} className="text-xs text-slate-500">
        {hint}
      </p>
    </div>
  );
}

function Problems({
  problems,
  kind,
}: {
  problems: readonly FormProblem[];
  kind: OriginalKind;
}) {
  const { t } = useTranslation();
  return (
    <div role="alert" className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
      <p className="font-medium">{t("addOriginal.problems.heading")}</p>
      <ul className="mt-1 list-disc pl-5">
        {problems.map((problem) => (
          <li key={problem}>{t(problemKey(problem, kind))}</li>
        ))}
      </ul>
    </div>
  );
}

function ProgressBar({ label, value, detail }: { label: string; value: number; detail?: string }) {
  return (
    <div>
      <div className="flex flex-wrap justify-between gap-2 text-sm">
        <span>{label}</span>
        <span className="text-slate-600">{detail ? `${detail} · ${value}%` : `${value}%`}</span>
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={value}
        className="mt-1 h-2 w-full overflow-hidden rounded bg-slate-200"
      >
        <div className="h-full bg-sky-700 transition-[width]" style={{ width: `${value}%` }} />
      </div>
    </div>
  );
}
