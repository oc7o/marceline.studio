"use client";
/**
 * "Send me a password-reset link" (issue #149, ADR 0054).
 *
 * The one thing this island must never do is tell the reader whether the address has an account:
 * Django answers the same 202 either way, and this renders the same sentence for every answer it
 * accepts. So there is no error state for "unknown address" — there cannot be one — and the success
 * text is deliberately conditional in its wording ("if this address has an account").
 */
import { type FormEvent, useState } from "react";

import { csrfToken } from "../lib/csrf";
import { type Resource, translator } from "../lib/translate";

export function ForgotPasswordForm({
  language,
  words,
}: {
  language: string;
  words: Resource;
}) {
  const t = translator(words, language);
  const [email, setEmail] = useState("");
  const [pending, setPending] = useState(false);
  const [sent, setSent] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    void (async () => {
      setPending(true);
      setProblem(null);
      try {
        const response = await fetch("/app/api/password-reset", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() ?? "" },
          body: JSON.stringify({ email, language }),
        });
        if (response.ok) setSent(true);
        else if (response.status === 403) setProblem("formExpired");
        else if (response.status === 429) setProblem("rateLimited");
        else if (response.status === 503) setProblem("unavailable");
        else setProblem("generic");
      } catch {
        setProblem("generic");
      } finally {
        setPending(false);
      }
    })();
  };

  if (sent) {
    return (
      <div>
        <h2 className="font-display text-display-s text-ink">{t("accountEmail.sent.heading")}</h2>
        <p className="mt-2 text-body text-ink-muted">{t("accountEmail.sent.body")}</p>
        <a
          href="/app/sign-in"
          className="tap mt-5 inline-flex rounded-sm text-body text-brand-ink underline"
        >
          {t("accountEmail.backToSignIn")}
        </a>
      </div>
    );
  }

  return (
    <form onSubmit={submit}>
      <h1 className="font-display text-display-m text-ink">{t("accountEmail.forgot.heading")}</h1>
      <p className="mt-2 mb-4 text-body text-ink-muted">{t("accountEmail.forgot.lead")}</p>
      <label className="mb-4 block">
        <span className="mb-1 block text-label text-ink-muted">
          {t("accountEmail.forgot.label")}
        </span>
        <input
          className="w-full rounded-sm border border-border-control bg-surface-100 px-3 py-2 text-body text-ink"
          type="email"
          inputMode="email"
          autoComplete="username"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
          autoFocus
        />
      </label>
      {problem === null ? null : (
        <p role="alert" className="mb-3 text-body-s text-critical-ink">
          {t(`accountEmail.errors.${problem}`)}
        </p>
      )}
      <button
        type="submit"
        disabled={pending}
        className="inline-flex w-full items-center justify-center gap-2 rounded-sm border border-brand bg-brand px-3 py-2 text-button text-on-brand shadow-sm transition-colors duration-120 ease-standard hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-45"
      >
        {pending ? t("accountEmail.forgot.submitting") : t("accountEmail.forgot.submit")}
      </button>
      <a
        href="/app/sign-in"
        className="tap mt-4 inline-flex rounded-sm text-body-s text-brand-ink underline"
      >
        {t("accountEmail.backToSignIn")}
      </a>
    </form>
  );
}
