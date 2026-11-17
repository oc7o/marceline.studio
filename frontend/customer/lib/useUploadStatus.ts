"use client";
/**
 * Ask what became of one upload, until there is nothing left to ask (ADR 0044 amendment
 * 2026-09-18).
 *
 * The file is sent by `uploadMedia.ts`; the fingerprinting then happens on our server, and this is
 * how the screen finds out how far it has come. Everything it decides — the schedule, when to stop
 * — is `@shared/app/uploadStatus`, so the behaviour is tested as pure functions and this hook is
 * only the wiring.
 *
 * Three things it must not do, each of which has cost somebody an afternoon somewhere:
 * poll after a terminal state, poll a hidden tab (the server carries on either way, and a
 * forgotten tab would ask for hours), and keep polling after the component is gone.
 */
import { useEffect, useState } from "react";

import { type Upload, type UploadState, pollDelayMs, shouldPoll } from "@shared/app/uploadStatus";

/** After this many answers we cannot read in a row, the screen says so and stops asking. */
export const MAX_FAILURES = 5;

export interface UploadWatch {
  /** The last status we read, or null before the first answer. */
  upload: Upload | null;
  /** Whether the status could not be read often enough to keep trying. */
  unreachable: boolean;
}

export function useUploadStatus(uploadId: string | null): UploadWatch {
  const [upload, setUpload] = useState<Upload | null>(null);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    if (uploadId === null) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    let failures = 0;
    let status: UploadState | null = null;
    const controller = new AbortController();

    const hidden = () => document.visibilityState === "hidden";

    const schedule = () => {
      if (stopped || !shouldPoll({ status, hidden: hidden() })) return;
      timer = setTimeout(() => void ask(), pollDelayMs(attempt));
    };

    const ask = async () => {
      if (stopped || hidden()) return;
      attempt += 1;
      try {
        const response = await fetch(`/app/api/uploads/${uploadId}`, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(String(response.status));
        const answer = (await response.json()) as Upload;
        if (stopped) return;
        failures = 0;
        status = answer.status;
        setUnreachable(false);
        setUpload(answer);
      } catch {
        if (stopped) return;
        failures += 1;
        if (failures >= MAX_FAILURES) {
          setUnreachable(true);
          return;
        }
      }
      schedule();
    };

    const resume = () => {
      if (stopped || hidden()) return;
      if (timer !== undefined) clearTimeout(timer);
      void ask();
    };

    document.addEventListener("visibilitychange", resume);
    void ask();
    return () => {
      stopped = true;
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [uploadId]);

  return { upload, unreachable };
}
