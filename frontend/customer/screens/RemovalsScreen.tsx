/**
 * The removals log (`/app/removals`): the account's receipt of everything that came down and when.
 *
 * Living state, not an archive (plan §8): an entry reopens when its copy comes back. "No copy
 * online" is the operator's definition with both halves — this one gone **and** nothing else of
 * the same original online — and the screen says, before the first entry, that this is only ever
 * as true as the sites we watch. It never lists those sites (operator, 2026-09-16): only the rough
 * size of our coverage, as the bucket `GET /app/me` sends.
 */
import { removalStanding } from "@shared/app/caseFile";
import { pageHref, pageLink, paginate } from "@shared/app/pagination";

import {
  BADGE,
  LoadError,
  TITLE_ROW,
  TitleRow,
  formatDate,
  formatDay,
} from "../components/parts";
import { Banner, EmptyState, Pagination } from "../components/ui";
import type { CustomerAccount, Removal } from "../lib/reads";
import { STANDING_TONES } from "../lib/tones";
import type { Words } from "../lib/words";

export function RemovalsScreen({
  i18n,
  removals,
  account,
  /** The `?page=` this log is shown at (issue #134); 1 when the page has no such query. */
  page = 1,
}: {
  i18n: Words;
  /** null when the read failed; the screen says so rather than showing an empty log. */
  removals: Removal[] | null;
  account: CustomerAccount;
  page?: number;
}) {
  const { t } = i18n;
  const paged = removals ? paginate(removals, page) : null;
  const previousPage = paged && paged.page > 1 ? paged.page - 1 : null;
  const nextPage = paged && paged.page < paged.pageCount ? paged.page + 1 : null;
  return (
    <section aria-labelledby="removals-heading" className="flex flex-col gap-4">
      <div className="flex flex-col gap-3">
        <div>
          <h1 id="removals-heading" className="font-display text-display-m text-ink">
            {t("removals.heading")}
          </h1>
          <p className="mt-1 max-w-[68ch] text-body text-ink-muted">{t("removals.lead")}</p>
        </div>
        {/* What "no copy online" can and cannot mean: standing context, so a note, not a status. */}
        <Banner role="note">
          {t("removals.watched")}{" "}
          {t("coverage.removals", {
            sources: t(`coverage.sources.${account.monitored_sources}`),
          })}
        </Banner>
      </div>
      {removals === null ? (
        <LoadError i18n={i18n} href="/app/removals" />
      ) : removals.length === 0 ? (
        // The whole view is this one state, so it may carry the view's Veil.
        <EmptyState title={t("removals.empty")} next={t("removals.emptyNext")} veil />
      ) : (
        <>
          <ul className="flex flex-col gap-2">
            {(paged?.items ?? removals).map((removal) => (
              <li key={`${removal.original_id}-${removal.page_id}`}>
                <RemovalEntry i18n={i18n} removal={removal} />
              </li>
            ))}
          </ul>
          {paged && paged.pageCount > 1 ? (
            <Pagination
              label={t("pagination.label")}
              status={t("pagination.status", {
                start: paged.start,
                end: paged.end,
                total: paged.total,
              })}
              previous={pageLink(t("pagination.previous"), pageHref("/app/removals", previousPage))}
              next={pageLink(t("pagination.next"), pageHref("/app/removals", nextPage))}
            />
          ) : null}
        </>
      )}
    </section>
  );
}

function RemovalEntry({ i18n, removal }: { i18n: Words; removal: Removal }) {
  const { t, language } = i18n;
  const stamp = (value: string) => formatDate(value, language);
  const day = (value: string) => formatDay(value, language);
  const standing = removalStanding(removal);

  return (
    <article
      aria-label={`${removal.title} · ${removal.site}`}
      className="rounded-none border border-hairline bg-surface-100 p-3"
    >
      {/* Only the title shares a row with the standing; site and address get the full width below. */}
      <TitleRow
        title={
          <div title={removal.title} className={`text-body-l font-medium text-ink ${TITLE_ROW.clamp}`}>
            {removal.title}
          </div>
        }
        badge={
          <span data-standing={standing} className={`${BADGE} ${STANDING_TONES[standing]}`}>
            {t(`removals.standing.${standing}`)}
          </span>
        }
      />
      <div className="mt-1 font-mono text-mono break-all text-ink">{removal.site}</div>
      {/* The address as text only: the record is where it can be opened, deliberately. */}
      <div className="font-mono text-mono-s break-all text-ink-subtle">{removal.url}</div>
      <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 text-caption text-ink-muted sm:grid-cols-3">
        <div>
          <dt className="inline">{t("removals.removedOn")}: </dt>
          <dd className="inline">{stamp(removal.removed_at)}</dd>
        </div>
        <div>
          <dt className="inline">{t("removals.confirmedOn")}: </dt>
          <dd className="inline">{day(removal.confirmed_on)}</dd>
        </div>
        <div>
          <dt className="inline">{t("removals.lastChecked")}: </dt>
          <dd className="inline">
            {removal.last_checked ? stamp(removal.last_checked) : t("removals.neverChecked")}
          </dd>
        </div>
      </dl>
      {removal.reappeared_at ? (
        <p className="mt-2 text-caption text-critical-ink">
          {t("removals.reappeared", { date: stamp(removal.reappeared_at) })}
        </p>
      ) : null}
      <a
        href={`/app/originals/${removal.original_id}/copies/${removal.page_id}`}
        className="tap mt-2 rounded-sm text-button text-brand-ink underline"
      >
        {t("finding.openCaseFile")}
      </a>
    </article>
  );
}
