/**
 * The removals log (`/app/removals`): the account's receipt of everything that came down and when.
 *
 * Living state, not an archive (plan §8): an entry reopens when its copy comes back. "Handled" is
 * the operator's definition with both halves — removed **and** nothing else of the same original
 * online — and the screen says, before the first entry, that this is only ever as true as the
 * sites we watch. It never lists those sites (operator decision, 2026-09-16): only the rough size of
 * our coverage, as the bucket `GET /app/me` sends.
 */
import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { type RemovalStanding, removalStanding } from "./caseFile";
import { BADGE, LoadError, Loading, TITLE_ROW, TitleRow, useFormatDate, useFormatDay } from "./parts";
import { type Removal, useCustomerAccount, useCustomerRemovals } from "./queries";

const STANDING_TONES: Record<RemovalStanding, string> = {
  handled: "bg-emerald-100 text-emerald-900",
  removed_case_open: "bg-sky-100 text-sky-900",
  reopened: "bg-rose-100 text-rose-900",
};

export function RemovalsScreen() {
  const { t } = useTranslation();
  const removals = useCustomerRemovals();
  // Only for the coverage bucket; the account is usually cached from the overview already.
  const account = useCustomerAccount();

  return (
    <section aria-labelledby="removals-heading" className="flex flex-col gap-3">
      <div>
        <h1 id="removals-heading" className="text-xl font-bold">
          {t("removals.heading")}
        </h1>
        <p className="mt-1 text-sm text-slate-600">{t("removals.lead")}</p>
        <p role="note" className="mt-2 rounded border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">
          {t("removals.watched")}
          {account.data ? (
            <> {t("coverage.removals", { sources: t(`coverage.sources.${account.data.monitored_sources}`) })}</>
          ) : null}
        </p>
      </div>
      {removals.isPending ? (
        <Loading />
      ) : removals.isError ? (
        <LoadError onRetry={() => void removals.refetch()} />
      ) : removals.data.length === 0 ? (
        <p className="rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-600">
          {t("removals.empty")}
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {removals.data.map((removal) => (
            <li key={`${removal.original_id}-${removal.page_id}`}>
              <RemovalEntry removal={removal} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function RemovalEntry({ removal }: { removal: Removal }) {
  const { t } = useTranslation();
  const formatDate = useFormatDate();
  const formatDay = useFormatDay();
  const standing = removalStanding(removal);

  return (
    <article aria-label={`${removal.title} · ${removal.site}`} className="rounded-lg border border-slate-200 bg-white p-3">
      {/* Only the title shares a row with the standing; site and address get the full width below. */}
      <TitleRow
        title={
          <div title={removal.title} className={`font-medium ${TITLE_ROW.clamp}`}>
            {removal.title}
          </div>
        }
        badge={
          <span data-standing={standing} className={`${BADGE} ${STANDING_TONES[standing]}`}>
            {t(`removals.standing.${standing}`)}
          </span>
        }
      />
      <div className="text-sm break-all text-slate-700">{removal.site}</div>
      {/* The address as text only: the case file is where it can be opened, deliberately. */}
      <div className="font-mono text-xs break-all text-slate-500">{removal.url}</div>
      <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 text-xs text-slate-600 sm:grid-cols-3">
        <div>
          <dt className="inline">{t("removals.removedOn")}: </dt>
          <dd className="inline">{formatDate(removal.removed_at)}</dd>
        </div>
        <div>
          <dt className="inline">{t("removals.confirmedOn")}: </dt>
          <dd className="inline">{formatDay(removal.confirmed_on)}</dd>
        </div>
        <div>
          <dt className="inline">{t("removals.lastChecked")}: </dt>
          <dd className="inline">
            {removal.last_checked ? formatDate(removal.last_checked) : t("removals.neverChecked")}
          </dd>
        </div>
      </dl>
      {removal.reappeared_at ? (
        <p className="mt-1 text-xs text-rose-900">{t("removals.reappeared", { date: formatDate(removal.reappeared_at) })}</p>
      ) : null}
      <Link
        to="/app/originals/$originalId/copies/$pageId"
        params={{ originalId: removal.original_id, pageId: removal.page_id }}
        className="mt-2 inline-block text-sm font-medium text-sky-800 underline"
      >
        {t("finding.openCaseFile")}
      </Link>
    </article>
  );
}
