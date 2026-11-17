/**
 * `/app` — the overview. Reads on the server, renders `HomeScreen`, ships no data-fetching code.
 *
 * The reads run together: the account is already in this request's cache (the gate above asked for
 * it), and the rest are independent of each other. A failed findings read costs the "where they
 * are" line, not the page; a failed rights read costs the rights notes; a failed account read means
 * Django is unreachable, and the gate above has already said so — this page then renders nothing
 * rather than an empty overview.
 */
import { pageFromParam } from "@shared/app/pagination";

import { requestI18n } from "../../lib/i18n";
import { optional } from "../../lib/pageReads";
import {
  readAccount,
  readFindings,
  readOriginals,
  readRemovals,
  readRights,
  readSubmissions,
  readUploads,
} from "../../lib/reads";
import { HomeScreen } from "../../screens/HomeScreen";

export default async function OverviewPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const { t, language } = await requestI18n();
  const { page } = await searchParams;
  const account = await optional(readAccount);
  if (account === null) return null;
  const [originals, findings, submissions, removals, rights, uploads] = await Promise.all([
    optional(readOriginals),
    optional(readFindings),
    account.can_add_originals ? optional(readSubmissions) : Promise.resolve(null),
    optional(readRemovals),
    // Development-only today (ADR 0034): a 404 here simply means no rights note on any card.
    optional(readRights),
    // Cloud fingerprinting (ADR 0044), development-only and off by default: the files this
    // organisation sent that are still being fingerprinted, or were not added.
    account.can_upload_media ? optional(readUploads) : Promise.resolve(null),
  ]);
  return (
    <HomeScreen
      i18n={{ t, language }}
      data={{ account, originals, findings, submissions, removals, rights, uploads }}
      page={pageFromParam(page)}
    />
  );
}
