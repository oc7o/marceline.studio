/**
 * Customer sign-in, speaking the contract of ADR 0023: a session GET for the CSRF cookie, then the
 * login POST, and one message per documented answer (200 / 401 / 403 / 429). The mapping lives in
 * `signInError.ts` so every branch is pinned by a test.
 *
 * There is no sign-up link and no "forgot password" link, because neither endpoint exists: the
 * operator creates accounts and sets passwords by hand
 * (#50). Saying so is friendlier than a link that 404s.
 */
import { Navigate, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../components/ui";
import { useCustomerLogin, useCustomerSession } from "./queries";
import { signInMessage } from "./signInError";

export function SignInScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const session = useCustomerSession();
  const login = useCustomerLogin();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    login.mutate(
      { username, password },
      { onSuccess: () => void navigate({ to: "/app", replace: true }) },
    );
  };

  // A bookmarked sign-in URL, or a second tab that signed in meanwhile.
  if (session.data?.authenticated === true) return <Navigate to="/app" replace />;

  const message = login.isError ? signInMessage(login.error) : null;
  return (
    <form
      onSubmit={submit}
      className="mx-auto w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
    >
      <h1 className="text-xl font-bold">{t("signIn.heading")}</h1>
      <p className="mt-2 mb-4 text-sm text-slate-600">{t("signIn.lead")}</p>
      <label className="mb-3 block text-sm">
        <span className="mb-1 block text-slate-600">{t("signIn.username")}</span>
        <input
          className="w-full rounded border border-slate-300 px-3 py-2"
          type="text"
          inputMode="email"
          autoComplete="username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          required
          autoFocus
        />
      </label>
      <label className="mb-4 block text-sm">
        <span className="mb-1 block text-slate-600">{t("signIn.password")}</span>
        <input
          className="w-full rounded border border-slate-300 px-3 py-2"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />
      </label>
      {message ? (
        <p role="alert" className="mb-3 text-sm text-rose-800">
          {t(message.key, message.values)}
        </p>
      ) : null}
      <Button
        type="submit"
        tone="primary"
        disabled={login.isPending}
        className="w-full justify-center"
      >
        {login.isPending ? t("signIn.submitting") : t("signIn.submit")}
      </Button>
      <p className="mt-3 text-xs text-slate-500">{t("signIn.accountsNote")}</p>
    </form>
  );
}
