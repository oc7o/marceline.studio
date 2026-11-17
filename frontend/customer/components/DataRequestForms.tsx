"use client";
/**
 * The three writes of the data-request screen (ADR 0056, issue #133): ask for a copy, ask for a
 * deletion, take either back.
 *
 * An island for one reason: each is a form, and after any of them the server has to be asked for the
 * page again (`router.refresh()`), because where a request stands is the server's answer and not
 * this component's state. Everything a browser does not need — the heading, the deadline, the
 * address, the history — is rendered by `DataRequestsScreen` on the server.
 *
 * Two things are deliberate here and are the reason this is not two buttons:
 *
 * - **the deletion asks for a typed word first**, the one the server sent (`deletion_phrase`), and
 *   the button stays disabled until it matches. The word is not translated: the document a public
 *   host serves from the signed bundle is language-independent, so the check has to be too;
 * - **an open request shows "take it back", not a second "ask again".** The server refuses a second
 *   open request of a kind (`already_open`), and a button that always fails is worse than none.
 *
 * On production a write answers 202 (ADR 0028 write path): the factual "received, it will show here
 * shortly", never a claim that anything happened.
 */
import { type FormEvent, useState } from "react";

import { ApiError } from "@shared/api/errors";

import type { DataRequest, DataRequests } from "../lib/reads";
import { type Resource, type Translate, translator } from "../lib/translate";
import { useWrite } from "../lib/write";

const INPUT =
  "mt-1 block w-full rounded-sm border border-border-control bg-surface-100 px-2 py-1.5 text-body-s text-ink";
const LABEL = "block text-label text-ink";
const BUTTON =
  "tap inline-flex items-center gap-2 rounded-sm border px-3 py-1.5 text-button transition-colors duration-120 ease-standard disabled:cursor-not-allowed disabled:opacity-45";
const PRIMARY = `${BUTTON} border-brand bg-brand text-on-brand hover:bg-brand-hover`;
const NEUTRAL = `${BUTTON} border-border-control bg-surface-100 text-ink hover:bg-surface-300`;
const CARD = "rounded-md border border-hairline bg-surface-100 p-4";

/** Refusal codes with a sentence of their own; anything else reads `dataRequests.errors.generic`. */
const CODES = [
  "already_open",
  "not_open",
  "confirmation_mismatch",
  "unknown_kind",
  "data_request_write_unavailable",
  "processing_paused",
  "too_many_requests",
] as const;

function errorKey(error: unknown): string {
  if (error instanceof ApiError && error.status !== 404) {
    const code = error.message;
    if ((CODES as readonly string[]).includes(code)) return `dataRequests.errors.${code}`;
  }
  return "dataRequests.errors.generic";
}

/** A 202 from the public host: accepted into the outbox, not applied yet. */
function isPending(answer: unknown): boolean {
  return (
    typeof answer === "object" &&
    answer !== null &&
    "pending" in answer &&
    (answer as { pending?: unknown }).pending === true
  );
}

function openOf(status: DataRequests, kind: DataRequest["kind"]): DataRequest | null {
  return status.requests.find((row) => row.kind === kind && row.open) ?? null;
}

export function DataRequestForms({
  language,
  words,
  status,
}: {
  language: string;
  words: Resource;
  status: DataRequests;
}) {
  const t = translator(words, language);
  return (
    <div className="flex flex-col gap-4">
      <ExportRequest t={t} language={language} open={openOf(status, "export")} />
      <DeletionRequest
        t={t}
        language={language}
        open={openOf(status, "deletion")}
        phrase={status.deletion_phrase}
      />
    </div>
  );
}

function ExportRequest({
  t,
  language,
  open,
}: {
  t: Translate;
  language: string;
  open: DataRequest | null;
}) {
  const write = useWrite();
  return (
    <section className={CARD} aria-labelledby="data-export-heading">
      <h2 id="data-export-heading" className="text-label text-ink-muted">
        {t("dataRequests.export.heading")}
      </h2>
      <p className="mt-1 max-w-[68ch] text-body-s text-ink">{t("dataRequests.export.body")}</p>
      {open === null ? (
        <button
          type="button"
          className={`${PRIMARY} mt-3`}
          disabled={write.pending}
          onClick={() => {
            void write.run("/app/api/data-requests/export", "POST");
          }}
        >
          {write.pending ? t("dataRequests.export.submitting") : t("dataRequests.export.submit")}
        </button>
      ) : (
        <OpenRequest
          t={t}
          language={language}
          request={open}
          kindKey="export"
          withdrawPath="/app/api/data-requests/export/withdraw"
        />
      )}
      <Answer t={t} write={write} />
    </section>
  );
}

function DeletionRequest({
  t,
  language,
  open,
  phrase,
}: {
  t: Translate;
  language: string;
  open: DataRequest | null;
  phrase: string;
}) {
  const write = useWrite();
  const [typed, setTyped] = useState("");
  // The server checks this too — twice, in fact, because a write from the public host is re-judged
  // where it is applied. Here it only keeps the button from sending something that will be refused.
  const matches = typed.trim() === phrase;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!matches) return;
    const sent = await write.run("/app/api/data-requests/deletion", "POST", { confirm: typed.trim() });
    if (sent) setTyped("");
  }

  return (
    <section className={CARD} aria-labelledby="data-deletion-heading">
      <h2 id="data-deletion-heading" className="text-label text-ink-muted">
        {t("dataRequests.deletion.heading")}
      </h2>
      <p className="mt-1 max-w-[68ch] text-body-s text-ink">{t("dataRequests.deletion.body")}</p>
      <p className="mt-1 max-w-[68ch] text-body-s text-ink-muted">
        {t("dataRequests.deletion.kept")}
      </p>
      {open === null ? (
        <form onSubmit={submit} className="mt-3 sm:max-w-md">
          <p className="max-w-[68ch] text-body-s text-ink">
            {t("dataRequests.deletion.phrase", { phrase })}
          </p>
          <label className={`${LABEL} mt-2`} htmlFor="data-deletion-confirm">
            {t("dataRequests.deletion.phraseLabel")}
          </label>
          <input
            id="data-deletion-confirm"
            className={INPUT}
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            maxLength={64}
            autoComplete="off"
            spellCheck={false}
          />
          <button type="submit" className={`${PRIMARY} mt-3`} disabled={write.pending || !matches}>
            {write.pending
              ? t("dataRequests.deletion.submitting")
              : t("dataRequests.deletion.submit")}
          </button>
        </form>
      ) : (
        <OpenRequest
          t={t}
          language={language}
          request={open}
          kindKey="deletion"
          withdrawPath="/app/api/data-requests/deletion/withdraw"
        />
      )}
      <Answer t={t} write={write} />
    </section>
  );
}

/** An open request: where it stands, when the answer is due, and the way to take it back. */
function OpenRequest({
  t,
  language,
  request,
  kindKey,
  withdrawPath,
}: {
  t: Translate;
  language: string;
  request: DataRequest;
  kindKey: "export" | "deletion";
  /** The BFF path, written out by the caller. A computed one would make `lib/routes.test.ts`'s
   * "every write an island sends has a handler" check vacuous for this screen. */
  withdrawPath: string;
}) {
  const write = useWrite();
  const stamp = (value: string) =>
    new Intl.DateTimeFormat(language, { dateStyle: "medium" }).format(new Date(value));
  return (
    <div className="mt-3" data-status={request.status}>
      <p className="text-body-s text-ink">
        {t(`dataRequests.${kindKey}.open`, { date: stamp(request.requested_at) })}
      </p>
      <p className="mt-1 text-body-s text-ink-muted">
        {t("dataRequests.status.heading")}: {t(`dataRequests.status.${request.status}`)}
      </p>
      <p className="mt-1 text-caption text-ink-subtle">
        {t("dataRequests.due", { date: stamp(request.due_at) })}
      </p>
      <button
        type="button"
        className={`${NEUTRAL} mt-3`}
        disabled={write.pending}
        onClick={() => {
          void write.run(withdrawPath, "POST");
        }}
      >
        {write.pending ? t("dataRequests.withdrawing") : t("dataRequests.withdraw")}
      </button>
      <Answer t={t} write={write} />
    </div>
  );
}

function Answer({ t, write }: { t: Translate; write: ReturnType<typeof useWrite> }) {
  if (write.error) {
    return (
      <p role="alert" className="mt-2 text-body-s text-critical-ink">
        {t(errorKey(write.error))}
      </p>
    );
  }
  if (isPending(write.data)) {
    return (
      <p role="status" className="mt-2 text-body-s text-ink-muted">
        {t("dataRequests.pending")}
      </p>
    );
  }
  return null;
}
