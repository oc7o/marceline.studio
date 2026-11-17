"use client";
/**
 * Setting a new password from a mailed link (issue #149, ADR 0054).
 *
 * Two endings, and the server says which: on development the password is set (`pending: false`), on
 * the public host the write is accepted and applied a minute later (`pending: true`) — the same shape
 * as the invite's last step, and for the same reason (ADR 0028). **Neither signs anybody in**: the
 * link proves the reader has the inbox, not that they know the password they just chose, so both
 * endings point at the sign-in form.
 *
 * Refusals are codes (`{"problems": ["password_too_short"]}`) and the words are ours.
 */
import { type FormEvent, useState } from "react";

import { csrfToken } from "../lib/csrf";
import { type Resource, translator } from "../lib/translate";

const KNOWN_PROBLEMS = [
  "password_too_short",
  "password_too_common",
  "password_entirely_numeric",
  "password_too_similar",
  "password_invalid",
];

/** Whether the answer says the write was accepted rather than applied (the public host). */
function saysPending(body: unknown): boolean {
  return (
    body !== null &&
    typeof body === "object" &&
    "pending" in body &&
    (body as { pending?: unknown }).pending === true
  );
}

export function ResetPasswordForm({
  language,
  token,
  minLength,
  words,
}: {
  language: string;
  token: string;
  minLength: number;
  words: Resource;
}) {
  const t = translator(words, language);
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [done, setDone] = useState<"set" | "accepted" | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    void (async () => {
      setPending(true);
      setProblems([]);
      try {
        const response = await fetch(`/app/api/password-reset/${token}`, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() ?? "" },
          body: JSON.stringify({ password }),
        });
        const parsed: unknown = await response.json().catch(() => null);
        if (response.ok) {
          setDone(saysPending(parsed) ? "accepted" : "set");
          return;
        }
        const listed =
          parsed && typeof parsed === "object" && "problems" in parsed && Array.isArray(parsed.problems)
            ? (parsed.problems as string[])
            : [];
        if (listed.length > 0) setProblems(listed);
        else if (response.status === 403) setProblems(["formExpired"]);
        else if (response.status === 429) setProblems(["rateLimited"]);
        else if (response.status === 503) setProblems(["unavailable"]);
        else if (response.status === 404) setProblems(["linkGone"]);
        else setProblems(["generic"]);
      } catch {
        setProblems(["generic"]);
      } finally {
        setPending(false);
      }
    })();
  };

  if (done !== null) {
    return (
      <div>
        <h2 className="font-display text-display-s text-ink">
          {t(done === "set" ? "accountEmail.reset.done" : "accountEmail.reset.accepted")}
        </h2>
        <p className="mt-2 text-body text-ink-muted">
          {t(done === "set" ? "accountEmail.reset.doneBody" : "accountEmail.reset.acceptedBody")}
        </p>
        <a
          href="/app/sign-in"
          className="tap mt-5 w-full justify-center gap-1 rounded-sm border border-brand bg-brand px-3 py-1.5 text-button text-on-brand shadow-sm transition-colors duration-120 ease-standard hover:bg-brand-hover sm:w-auto sm:justify-start"
        >
          {t("accountEmail.backToSignIn")}
        </a>
      </div>
    );
  }

  const message = (code: string): string =>
    KNOWN_PROBLEMS.includes(code)
      ? t(`accountEmail.password.errors.${code}`, { count: minLength })
      : t(`accountEmail.errors.${code}`);

  return (
    <form onSubmit={submit}>
      <label className="mb-2 block">
        <span className="mb-1 block text-label text-ink-muted">
          {t("accountEmail.reset.label")}
        </span>
        <input
          className="w-full rounded-sm border border-border-control bg-surface-100 px-3 py-2 text-body text-ink"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
          autoFocus
        />
      </label>
      <p className="mb-4 text-caption text-ink-subtle">
        {t("accountEmail.reset.requirement", { count: minLength })}
      </p>
      {problems.length === 0 ? null : (
        <ul role="alert" className="mb-3 space-y-1 text-body-s text-critical-ink">
          {problems.map((code) => (
            <li key={code}>{message(code)}</li>
          ))}
        </ul>
      )}
      <button
        type="submit"
        disabled={pending}
        className="inline-flex w-full items-center justify-center gap-2 rounded-sm border border-brand bg-brand px-3 py-2 text-button text-on-brand shadow-sm transition-colors duration-120 ease-standard hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-45"
      >
        {pending ? t("accountEmail.reset.submitting") : t("accountEmail.reset.submit")}
      </button>
    </form>
  );
}
