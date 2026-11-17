/**
 * `/app/verification` (ADR 0055): showing us that the platform account is yours.
 *
 * A 404 from Django means the step is not switched on here (`ACCOUNT_CONTROL_VERIFICATION_ENABLED`),
 * which is a sentence and not an error — the same shape as `/app/rights` when production does not
 * serve rights yet. `me.can_verify_account` is what the other screens use to decide whether to link
 * here at all.
 */
import { redirect } from "next/navigation";

import { LoadError } from "../../../components/parts";
import { Banner } from "../../../components/ui";
import { VerificationScreen, verificationWords } from "../../../screens/VerificationScreen";
import { isNotFound, isSignedOut } from "../../../lib/django";
import { requestI18n } from "../../../lib/i18n";
import { type VerificationStatus, readVerification } from "../../../lib/reads";

export default async function VerificationPage() {
  const { t, language, resource } = await requestI18n();
  let status: VerificationStatus | null = null;
  let notOnline = false;
  try {
    status = await readVerification();
  } catch (error) {
    if (isSignedOut(error)) redirect("/sign-in");
    notOnline = isNotFound(error);
  }

  if (status === null) {
    return (
      <section aria-labelledby="verification-heading" className="flex flex-col gap-4">
        <div>
          <h1 id="verification-heading" className="font-display text-display-m text-ink">
            {t("verification.heading")}
          </h1>
          <p className="mt-1 max-w-[68ch] text-body text-ink-muted">{t("verification.lead")}</p>
        </div>
        {notOnline ? (
          <Banner role="note">{t("verification.notOnline")}</Banner>
        ) : (
          <LoadError i18n={{ t, language }} href="/app/verification" />
        )}
      </section>
    );
  }

  return (
    <VerificationScreen
      i18n={{ t, language }}
      words={verificationWords(resource)}
      status={status}
    />
  );
}
