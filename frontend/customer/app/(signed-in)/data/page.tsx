/**
 * `/app/data` (ADR 0056, issue #133): asking for a copy of your data, or for its deletion.
 *
 * A 404 from Django means the step is not switched on here (`CUSTOMER_DATA_REQUESTS_ENABLED`),
 * which is a sentence and not an error — the footer's address still reaches a person, and that is
 * what the sentence says. `me.can_request_data` is what the shell asks before linking here.
 */
import { redirect } from "next/navigation";

import { LoadError } from "../../../components/parts";
import { Banner } from "../../../components/ui";
import { DataRequestsScreen, dataRequestWords } from "../../../screens/DataRequestsScreen";
import { isNotFound, isSignedOut } from "../../../lib/django";
import { requestI18n } from "../../../lib/i18n";
import { type DataRequests, readDataRequests } from "../../../lib/reads";

export default async function DataRequestsPage() {
  const { t, language, resource } = await requestI18n();
  let status: DataRequests | null = null;
  let notOnline = false;
  try {
    status = await readDataRequests();
  } catch (error) {
    if (isSignedOut(error)) redirect("/sign-in");
    notOnline = isNotFound(error);
  }

  if (status === null) {
    return (
      <section aria-labelledby="data-heading" className="flex flex-col gap-4">
        <div>
          <h1 id="data-heading" className="font-display text-display-m text-ink">
            {t("dataRequests.heading")}
          </h1>
          <p className="mt-1 max-w-[68ch] text-body text-ink-muted">{t("dataRequests.lead")}</p>
        </div>
        {notOnline ? (
          <Banner role="note">{t("dataRequests.notOnline")}</Banner>
        ) : (
          <LoadError i18n={{ t, language }} href="/app/data" />
        )}
      </section>
    );
  }

  return (
    <DataRequestsScreen
      i18n={{ t, language }}
      words={dataRequestWords(resource)}
      status={status}
    />
  );
}
