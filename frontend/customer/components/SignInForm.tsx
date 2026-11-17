"use client";
/**
 * The sign-in form, speaking the contract of ADR 0023 exactly as the SPA did: a session GET for
 * the CSRF cookie, then the login POST, and one message per documented answer (200 / 401 / 403 /
 * 429). The mapping is `@shared/app/signInError`, unchanged and still pinned by its own tests.
 *
 * Both requests go to this server's own route handlers, which forward them to Django with the
 * browser's cookies (ADR 0036). There is no sign-up link — accounts are issued by invite (ADR 0052,
 * ADR 0023) — but there **is** a "forgotten your password?" link since ADR 0054, and the note below
 * it says the rest: we set accounts up ourselves.
 */
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { ApiError } from "@shared/api/errors";
import { signInMessage } from "@shared/app/signInError";

import { csrfToken } from "../lib/csrf";
import { type Resource, translator } from "../lib/translate";

export function SignInForm({ language, words }: { language: string; words: Resource }) {
  const t = translator(words, language);
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      // The session GET first, for the CSRF cookie: nothing else sets it, and the POST is refused
      // without it (ADR 0023).
      await fetch("/app/api/session", { credentials: "same-origin" });
      const response = await fetch("/app/api/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() ?? "" },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        const detail =
          body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
            ? body.detail
            : `Request failed (${response.status})`;
        const retryAfter = response.headers.get("Retry-After");
        throw new ApiError(
          response.status,
          detail,
          body,
          retryAfter && /^\d+$/.test(retryAfter) ? Number(retryAfter) : null,
        );
      }
      // Signed in. The overview is rendered by the server, so ask it again rather than navigating
      // with data this page happens to hold.
      router.replace("/");
      router.refresh();
    } catch (caught) {
      setError(caught);
      setPending(false);
    }
  };

  const message = error === null ? null : signInMessage(error);
  return (
    <form
      onSubmit={(event) => void submit(event)}
      className="mx-auto w-full max-w-sm rounded-md border border-hairline bg-surface-100 p-6 shadow-md"
    >
      <h1 className="font-display text-display-m text-ink">{t("signIn.heading")}</h1>
      <p className="mt-2 mb-5 text-body text-ink-muted">{t("signIn.lead")}</p>
      <label className="mb-3 block">
        <span className="mb-1 block text-label text-ink-muted">{t("signIn.username")}</span>
        <input
          className="w-full rounded-sm border border-border-control bg-surface-100 px-3 py-2 text-body text-ink"
          type="text"
          inputMode="email"
          autoComplete="username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          required
          autoFocus
        />
      </label>
      <label className="mb-5 block">
        <span className="mb-1 block text-label text-ink-muted">{t("signIn.password")}</span>
        <input
          className="w-full rounded-sm border border-border-control bg-surface-100 px-3 py-2 text-body text-ink"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />
      </label>
      {message ? (
        <p role="alert" className="mb-3 text-body-s text-critical-ink">
          {t(message.key, message.values)}
        </p>
      ) : null}
      <button
        type="submit"
        disabled={pending}
        className="inline-flex w-full items-center justify-center gap-2 rounded-sm border border-brand bg-brand px-3 py-2 text-button text-on-brand shadow-sm transition-colors duration-120 ease-standard hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-45"
      >
        {pending ? t("signIn.submitting") : t("signIn.submit")}
      </button>
      {/* The one link out of this form (ADR 0054). A plain `<a>`, so it carries `basePath`, and
          `.tap` for the 44 px hit area a finger needs. */}
      <a href="/app/forgot-password" className="tap mt-4 inline-flex rounded-sm text-body-s text-brand-ink underline">
        {t("signIn.forgot")}
      </a>
      <p className="mt-3 text-caption text-ink-subtle">{t("signIn.accountsNote")}</p>
    </form>
  );
}
