/**
 * The session gate, and the only place it is applied (ADR 0036; ADR 0026's `app-protected`).
 *
 * Every screen except sign-in is a page inside this route group, so a new screen is behind the
 * gate **by construction** rather than by remembering to check. `lib/routes.test.ts` fails the
 * build if a page appears outside the group.
 *
 * The gate asks Django (`GET /app/me`), never a token this server decided to believe: production's
 * customer API is the authority on every call (ADR 0028 c). Its three answers:
 *
 * - **401** — redirect to sign-in.
 * - **anything else** (Django down, a 500, a timeout) — the shell with "we could not reach the
 *   server", in the customer's language. Never an empty overview: "nothing was found" is a
 *   statement about their work, and a failed read must not be able to make it.
 * - **200** — the shell with the screen inside it.
 */
import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { Shell } from "../../components/Shell";
import { isSignedOut } from "../../lib/django";
import { requestI18n } from "../../lib/i18n";
import { type CustomerAccount, readAccount } from "../../lib/reads";

export default async function SignedInLayout({ children }: { children: ReactNode }) {
  const { t, language } = await requestI18n();
  const i18n = { t, language };

  let account: CustomerAccount | null = null;
  try {
    account = await readAccount();
  } catch (error) {
    if (isSignedOut(error)) redirect("/sign-in");
  }

  if (account === null) {
    return (
      <Shell i18n={i18n} username={null}>
        <p role="alert" className="text-body text-critical-ink">
          {t("common.unreachable")}
        </p>
      </Shell>
    );
  }

  return (
    <Shell
      i18n={i18n}
      username={account.username}
      canExportData={account.can_export_data}
      canRequestData={account.can_request_data}
      // ADR 0054 §3: ask, once, quietly. Only where a confirmation can actually be asked for.
      askToVerifyEmail={!account.email_verified && account.can_verify_email}
    >
      {children}
    </Shell>
  );
}
