/**
 * "I forgot my password": `/app/forgot-password` (issue #149, ADR 0054).
 *
 * Outside the session gate by definition — somebody who can sign in does not need it. It reads
 * nothing: the form posts an address and Django answers the same 202 whatever that address is, so
 * there is nothing this server could ask and nothing it should know.
 */
import type { Metadata } from "next";

import { ForgotPasswordScreen } from "../../screens/AccountEmailScreen";
import { requestI18n } from "../../lib/i18n";
import { slice } from "../../lib/translate";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  robots: { index: false, follow: false },
};

export default async function ForgotPasswordPage() {
  const { t, language, resource } = await requestI18n();
  return (
    <ForgotPasswordScreen
      i18n={{ t, language }}
      words={slice(resource, "accountEmail", "common", "signIn")}
    />
  );
}
