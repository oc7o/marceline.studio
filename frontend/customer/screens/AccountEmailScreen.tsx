/**
 * The three screens of the mailed account actions (issue #149, ADR 0054).
 *
 * Synchronous server components as usual: data in as props, HTML out, so `screens.test.tsx` renders
 * them to a string without a request. Each one holds the **literal** BFF path its island posts to —
 * `lib/routes.test.ts` matches those against the handlers that exist, and it can only see inside a
 * string as far as the static parts go, so the path belongs here and not in a computed prop.
 *
 * `state === null` is the **neutral page**: unknown, malformed, expired, already used and "the feature
 * is off" all arrive as the same null and are rendered as the same paragraph. There is no branch to
 * add, because this server is not told which of them it was (ADR 0054, the rule of ADR 0052 §6).
 */
import type { ReactNode } from "react";

import type { Words } from "../lib/words";
import type { EmailVerificationState, PasswordResetState } from "../lib/reads";
import type { Resource } from "../lib/translate";
import { AccountMailButton } from "../components/AccountMailButton";
import { ForgotPasswordForm } from "../components/ForgotPasswordForm";
import { ResetPasswordForm } from "../components/ResetPasswordForm";
import { Shell } from "../components/Shell";

function Card({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto w-full max-w-md rounded-md border border-hairline bg-surface-100 p-6 shadow-md">
      {children}
    </div>
  );
}

function Unavailable({ i18n }: { i18n: Words }) {
  return (
    <Card>
      <h1 className="font-display text-display-m text-ink">
        {i18n.t("accountEmail.unavailable.heading")}
      </h1>
      <p className="mt-3 text-body text-ink-muted">{i18n.t("accountEmail.unavailable.body")}</p>
      <a
        href="/app/sign-in"
        className="tap mt-5 inline-flex rounded-sm text-body text-brand-ink underline"
      >
        {i18n.t("accountEmail.backToSignIn")}
      </a>
    </Card>
  );
}

/** "I forgot my password": the one screen that takes an address, and tells nobody anything. */
export function ForgotPasswordScreen({ i18n, words }: { i18n: Words; words: Resource }) {
  return (
    <Shell i18n={i18n} username={null}>
      <Card>
        <ForgotPasswordForm language={i18n.language} words={words} />
      </Card>
    </Shell>
  );
}

/** Setting a new password from a mailed link. */
export function ResetPasswordScreen({
  i18n,
  token,
  state,
  words,
}: {
  i18n: Words;
  token: string;
  state: PasswordResetState | null;
  words: Resource;
}) {
  if (state === null) return <Shell i18n={i18n} username={null}><Unavailable i18n={i18n} /></Shell>;
  return (
    <Shell i18n={i18n} username={null}>
      <Card>
        <h1 className="font-display text-display-m text-ink">
          {i18n.t("accountEmail.reset.heading")}
        </h1>
        <p className="mt-2 text-body text-ink-muted">{i18n.t("accountEmail.reset.lead")}</p>
        <p className="mt-4 text-label text-ink-subtle">{i18n.t("accountEmail.account")}</p>
        <p className="text-body text-ink">{state.email}</p>
        <hr className="my-5 border-hairline" />
        <ResetPasswordForm
          language={i18n.language}
          token={token}
          minLength={state.password_min_length}
          words={words}
        />
      </Card>
    </Shell>
  );
}

/** Confirming an address from a mailed link. The act is the button, never the visit. */
export function VerifyEmailScreen({
  i18n,
  token,
  state,
  words,
}: {
  i18n: Words;
  token: string;
  state: EmailVerificationState | null;
  words: Resource;
}) {
  if (state === null) return <Shell i18n={i18n} username={null}><Unavailable i18n={i18n} /></Shell>;
  return (
    <Shell i18n={i18n} username={null}>
      <Card>
        <h1 className="font-display text-display-m text-ink">
          {i18n.t("accountEmail.confirm.heading")}
        </h1>
        <p className="mt-2 text-body text-ink-muted">{i18n.t("accountEmail.confirm.lead")}</p>
        <p className="mt-4 text-label text-ink-subtle">{i18n.t("accountEmail.account")}</p>
        <p className="mb-5 text-body text-ink">{state.email}</p>
        <AccountMailButton
          language={i18n.language}
          path={`/app/api/verify-email/${token}`}
          label={i18n.t("accountEmail.confirm.submit")}
          busyLabel={i18n.t("accountEmail.confirm.submitting")}
          doneHeading={i18n.t("accountEmail.confirm.done")}
          doneBody={i18n.t("accountEmail.confirm.doneBody")}
          words={words}
        />
      </Card>
    </Shell>
  );
}

/**
 * "Send that mail again", from the banner. Inside the session gate, so the shell is the signed-in
 * one and the page can say the address without being told it.
 */
export function ResendVerificationScreen({
  i18n,
  username,
  canVerify,
  words,
}: {
  i18n: Words;
  username: string;
  canVerify: boolean;
  words: Resource;
}) {
  return (
    <Shell i18n={i18n} username={username}>
      <div className="mx-auto w-full max-w-md">
        <h1 className="font-display text-display-m text-ink">
          {i18n.t("accountEmail.resend.heading")}
        </h1>
        <p className="mt-2 text-body text-ink-muted">{i18n.t("accountEmail.resend.lead")}</p>
        <div className="mt-4 rounded-md border border-hairline bg-surface-100 p-6 shadow-md">
          <p className="text-label text-ink-subtle">{i18n.t("accountEmail.account")}</p>
          <p className="mb-5 text-body text-ink">{username}</p>
          {canVerify ? (
            <AccountMailButton
              language={i18n.language}
              path="/app/api/verify-email"
              label={i18n.t("accountEmail.resend.submit")}
              busyLabel={i18n.t("accountEmail.resend.submitting")}
              doneHeading={i18n.t("accountEmail.resend.done")}
              doneBody={i18n.t("accountEmail.resend.doneBody")}
              words={words}
            />
          ) : (
            <p className="text-body text-ink-muted">{i18n.t("accountEmail.resend.unavailable")}</p>
          )}
        </div>
      </div>
    </Shell>
  );
}
