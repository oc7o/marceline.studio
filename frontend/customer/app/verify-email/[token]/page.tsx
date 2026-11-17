/**
 * Confirming an address from a mailed link: `/app/verify-email/{token}` (issue #149, ADR 0054).
 *
 * Outside the session gate, because the link may be opened in any browser — the inbox is the proof,
 * not a session. Reading this page **confirms nothing**: mail clients, scanners and link previews
 * fetch links on their own, so the act is the button, which posts.
 */
import type { Metadata } from "next";

import { VerifyEmailScreen } from "../../../screens/AccountEmailScreen";
import { isNotFound } from "../../../lib/django";
import { requestI18n } from "../../../lib/i18n";
import { readEmailVerification } from "../../../lib/reads";
import { slice } from "../../../lib/translate";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  robots: { index: false, follow: false },
};

export default async function VerifyEmailPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const { t, language, resource } = await requestI18n();

  let state = null;
  try {
    state = await readEmailVerification(token);
  } catch (error) {
    if (!isNotFound(error)) state = null;
  }

  return (
    <VerifyEmailScreen
      i18n={{ t, language }}
      token={token}
      state={state}
      words={slice(resource, "accountEmail", "common", "signIn")}
    />
  );
}
