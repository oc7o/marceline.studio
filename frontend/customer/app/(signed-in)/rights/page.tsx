/**
 * `/app/rights` (ADR 0034).
 *
 * The heading and the "not shown here yet" case are server-rendered; the forms below them are one
 * island, which is what almost the whole screen is. A 404 means production does not serve rights
 * yet — not an error.
 */
import { redirect } from "next/navigation";

import { RightsScreen } from "../../../components/RightsScreen";
import { LoadError } from "../../../components/parts";
import { Banner } from "../../../components/ui";
import { isNotFound, isSignedOut } from "../../../lib/django";
import { optional } from "../../../lib/pageReads";
import { requestI18n } from "../../../lib/i18n";
import { type RightsOverview, readAccount, readRights } from "../../../lib/reads";
import { slice } from "../../../lib/translate";

export default async function RightsPage() {
  const { t, language, resource } = await requestI18n();
  let overview: RightsOverview | null = null;
  let notOnline = false;
  try {
    overview = await readRights();
  } catch (error) {
    if (isSignedOut(error)) redirect("/sign-in");
    notOnline = isNotFound(error);
  }
  // ADR 0055: the identity block points at the account-control step where that step exists. The
  // read is the gate's own (`readAccount` is memoised per request), so this costs no second call.
  const account = await optional(readAccount);

  if (overview === null) {
    return (
      <section aria-labelledby="rights-heading" className="flex flex-col gap-4">
        <div>
          <h1 id="rights-heading" className="font-display text-display-m text-ink">
            {t("rights.heading")}
          </h1>
          <p className="mt-1 max-w-[68ch] text-body text-ink-muted">{t("rights.lead")}</p>
        </div>
        {notOnline ? (
          <Banner role="note">{t("rights.notOnline")}</Banner>
        ) : (
          <LoadError i18n={{ t, language }} href="/app/rights" />
        )}
      </section>
    );
  }

  return (
    <RightsScreen
      language={language}
      words={slice(resource, "rights", "common")}
      overview={overview}
      accountStep={account?.can_verify_account ?? false}
    />
  );
}
