/** `/app/removals` — the removals log. */
import { pageFromParam } from "@shared/app/pagination";

import { requestI18n } from "../../../lib/i18n";
import { optional } from "../../../lib/pageReads";
import { readAccount, readRemovals } from "../../../lib/reads";
import { RemovalsScreen } from "../../../screens/RemovalsScreen";

export default async function RemovalsPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const { t, language } = await requestI18n();
  const { page } = await searchParams;
  // The account is already in this request's cache (the gate asked for it); it is read here only
  // for the coverage bucket. Null means Django is unreachable and the gate above has said so.
  const [account, removals] = await Promise.all([optional(readAccount), optional(readRemovals)]);
  if (account === null) return null;
  return (
    <RemovalsScreen
      i18n={{ t, language }}
      removals={removals}
      account={account}
      page={pageFromParam(page)}
    />
  );
}
