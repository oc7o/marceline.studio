"use client";
/**
 * One button that posts one account-mail write and turns the answer into a sentence (ADR 0054).
 *
 * Two callers, because they are the same shape: confirming an address from a mailed link
 * (`/app/api/verify-email/{token}`) and asking for that mail again (`/app/api/verify-email`). Both
 * answer 202 on the public host and 200 on development, and neither has anything to show but a line.
 *
 * It is an island because a confirmation must be a **POST** — a GET is fetched by mail clients,
 * scanners and link previews — and because the answer has to become a sentence without leaving the
 * page. The paths are passed in as literals by the screens, so `lib/routes.test.ts` still sees them.
 */
import { type FormEvent, useState } from "react";

import { csrfToken } from "../lib/csrf";
import { type Resource, translator } from "../lib/translate";

export function AccountMailButton({
  language,
  path,
  label,
  busyLabel,
  doneHeading,
  doneBody,
  words,
}: {
  language: string;
  /** The BFF path to post to. Written out literally by the caller, never computed here. */
  path: string;
  label: string;
  busyLabel: string;
  doneHeading: string;
  doneBody: string;
  words: Resource;
}) {
  const t = translator(words, language);
  const [pending, setPending] = useState(false);
  const [done, setDone] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    void (async () => {
      setPending(true);
      setProblem(null);
      try {
        const response = await fetch(path, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() ?? "" },
          body: JSON.stringify({ language }),
        });
        if (response.ok) setDone(true);
        else if (response.status === 403) setProblem("formExpired");
        else if (response.status === 429) setProblem("rateLimited");
        else if (response.status === 503) setProblem("unavailable");
        else if (response.status === 404) setProblem("linkGone");
        else setProblem("generic");
      } catch {
        setProblem("generic");
      } finally {
        setPending(false);
      }
    })();
  };

  if (done) {
    return (
      <div>
        <h2 className="font-display text-display-s text-ink">{doneHeading}</h2>
        <p className="mt-2 text-body text-ink-muted">{doneBody}</p>
      </div>
    );
  }

  return (
    <form onSubmit={submit}>
      {problem === null ? null : (
        <p role="alert" className="mb-3 text-body-s text-critical-ink">
          {t(`accountEmail.errors.${problem}`)}
        </p>
      )}
      <button
        type="submit"
        disabled={pending}
        className="inline-flex w-full items-center justify-center gap-2 rounded-sm border border-brand bg-brand px-3 py-2 text-button text-on-brand shadow-sm transition-colors duration-120 ease-standard hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-45 sm:w-auto"
      >
        {pending ? busyLabel : label}
      </button>
    </form>
  );
}
