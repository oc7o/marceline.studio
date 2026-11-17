/**
 * Setting a new password from a mailed link: `/app/reset-password/{token}` (issue #149, ADR 0054).
 *
 * Outside the session gate: whoever opens it cannot sign in, that being the whole point. Django
 * answers one identical 404 for unknown, malformed, expired, already used and "the feature is off",
 * so the read throws, the screen gets `state = null` and renders one paragraph. **Never branch on the
 * reason** — this server is not told it, and a second sentence would be an oracle about an address.
 */
import type { Metadata } from "next";

import { ResetPasswordScreen } from "../../../screens/AccountEmailScreen";
import { isNotFound } from "../../../lib/django";
import { requestI18n } from "../../../lib/i18n";
import { readPasswordReset } from "../../../lib/reads";
import { slice } from "../../../lib/translate";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  // A link that is forwarded or pasted must not become a search result.
  robots: { index: false, follow: false },
};

export default async function ResetPasswordPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const { t, language, resource } = await requestI18n();

  let state = null;
  try {
    state = await readPasswordReset(token);
  } catch (error) {
    if (!isNotFound(error)) {
      // Django unreachable, or throttled. The screen says the link cannot be opened; it never says
      // the link is bad, because this server does not know that.
      state = null;
    }
  }

  return (
    <ResetPasswordScreen
      i18n={{ t, language }}
      token={token}
      state={state}
      words={slice(resource, "accountEmail", "common", "signIn")}
    />
  );
}
