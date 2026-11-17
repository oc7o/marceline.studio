/**
 * "Send that mail again": `/app/verify-email` (issue #149, ADR 0054).
 *
 * Inside the gate — it is about the signed-in account, and the banner that links here is only shown
 * to one. `can_verify_email` false (the flows are off, or no transport is configured) renders a
 * sentence rather than a button that would answer 404.
 */
import { ResendVerificationScreen } from "../../../screens/AccountEmailScreen";
import { requestI18n } from "../../../lib/i18n";
import { readAccount } from "../../../lib/reads";
import { slice } from "../../../lib/translate";

export const dynamic = "force-dynamic";

export default async function ResendVerificationPage() {
  const { t, language, resource } = await requestI18n();
  const account = await readAccount();
  return (
    <ResendVerificationScreen
      i18n={{ t, language }}
      username={account.username}
      canVerify={account.can_verify_email}
      words={slice(resource, "accountEmail", "common")}
    />
  );
}
