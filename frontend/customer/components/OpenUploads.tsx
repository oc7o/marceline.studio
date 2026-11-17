/**
 * The files this organisation sent that are still being fingerprinted, or were not added
 * (issue #59, ADR 0044 amendment 2026-09-18).
 *
 * The customer may close the add page — the server carries on — so the overview and the add page
 * both show what is still in flight when they come back. A **synchronous server component**: the
 * page reads, this renders, and the only client code is the `Refresher`, and only while something
 * is actually moving.
 *
 * Their own uploads and nothing else: no review work of ours, no counts of anything they did not
 * send. A file that was added is simply an original in the list below it.
 */
import {
  type Upload,
  anyRunning,
  framesPercent,
  openUploads,
  statusKey,
} from "@shared/app/uploadStatus";

import type { Words } from "../lib/words";
import { Refresher } from "./Refresher";

/** How often the page asks the server again while a file is being fingerprinted. */
export const UPLOAD_POLL_MS = 10_000;

export function OpenUploads({ i18n, uploads }: { i18n: Words; uploads: Upload[] | null }) {
  const open = uploads ? openUploads(uploads) : [];
  if (open.length === 0) return null;
  return (
    <section
      aria-labelledby="uploads-heading"
      className="rounded-md border border-hairline bg-surface-100 p-4 text-body-s text-ink"
    >
      <h2 id="uploads-heading" className="font-display text-display-s text-ink">
        {i18n.t("addOriginal.upload.open.heading")}
      </h2>
      {anyRunning(open) ? <Refresher intervalMs={UPLOAD_POLL_MS} /> : null}
      <ul className="mt-2 flex flex-col gap-2">
        {open.map((upload) => {
          const frames = framesPercent(upload);
          return (
            <li key={upload.id} aria-label={upload.title}>
              <p className="flex flex-wrap items-center gap-2">
                <span className="font-medium break-words text-ink">{upload.title}</span>
                {frames === null ? null : (
                  <span className="font-mono text-mono-s text-ink-muted">{frames}%</span>
                )}
              </p>
              <p role={upload.reason ? undefined : "status"} className="text-ink-muted">
                {i18n.t(statusKey(upload))}
              </p>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
