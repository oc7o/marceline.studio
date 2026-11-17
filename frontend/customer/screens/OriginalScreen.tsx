/**
 * One original (`/app/originals/{orig_…}`): the state of its record and every matching copy found
 * of it, each linking to its own record.
 *
 * Reads `GET /api/v1/app/cases/{id}`, the same response the record uses, so moving between the two
 * costs no second request. A 404 — unknown original or another organisation's, deliberately the
 * same — renders the not-found page and nothing else.
 */
import { whereabouts } from "@shared/app/caseFile";

import { Card, StateBadge, TitleRow, WhereaboutsBadge, formatDate, formatDay } from "../components/parts";
import { Banner } from "../components/ui";
import type { CaseDetail } from "../lib/reads";
import type { Words } from "../lib/words";

export function OriginalScreen({ i18n, detail }: { i18n: Words; detail: CaseDetail }) {
  const { t, language } = i18n;
  const day = (value: string) => formatDay(value, language);
  const stamp = (value: string) => formatDate(value, language);
  const { original, findings } = detail;
  const caseRow = detail.case;

  return (
    <div className="flex flex-col gap-5">
      <div>
        <a href="/app" className="tap rounded-sm text-body-s text-brand-ink underline">
          {t("original.back")}
        </a>
        {/* The detail heading is the one place the whole title shows, so it wraps instead of clamping. */}
        <div className="mt-2">
          <TitleRow
            title={
              <h1 className="font-display text-display-m break-words text-ink">{original.title}</h1>
            }
            badge={<StateBadge state={original.state} i18n={i18n} />}
          />
        </div>
        <p className="mt-2 text-body-s text-ink-muted">
          {t("original.added", { date: day(original.created_at.slice(0, 10)) })}
        </p>
      </div>

      <Card labelledBy="case-heading">
        <h2 id="case-heading" className="font-display text-display-s text-ink">
          {t("original.case.heading")}
        </h2>
        {caseRow ? (
          <div className="mt-2 flex flex-col gap-1 text-body-s text-ink">
            <p>{t("original.case.opened", { date: day(caseRow.opened_on) })}</p>
            {caseRow.state === "removed_watching" && caseRow.removed_at ? (
              <p>{t("original.case.removedWatching", { date: stamp(caseRow.removed_at) })}</p>
            ) : (
              <p>{t("original.case.open")}</p>
            )}
            {caseRow.reopen_count > 0 && caseRow.reopened_on ? (
              <div className="mt-1">
                <Banner tone="caution" role="note">
                  {t("original.case.reopened", {
                    count: caseRow.reopen_count,
                    date: day(caseRow.reopened_on),
                  })}
                </Banner>
              </div>
            ) : null}
          </div>
        ) : (
          <p className="mt-2 text-body-s text-ink">{t("original.case.none")}</p>
        )}
      </Card>

      {findings.length > 0 ? (
        <section aria-labelledby="findings-heading" className="flex flex-col gap-3">
          <div>
            <h2 id="findings-heading" className="font-display text-display-s text-ink">
              {t("original.findings.heading")}
            </h2>
            <p className="mt-1 text-body-s text-ink-muted">{t("original.findings.lead")}</p>
          </div>
          {/*
            The copies are evidence, so they read as one dense, square-cornered record rather than a
            stack of soft cards: one hairline around the list, one between the rows.
          */}
          <ul className="flex flex-col rounded-none border border-hairline bg-surface-100 divide-y divide-hairline">
            {findings.map((finding) => (
              <li key={finding.page_id} className="px-3 py-2">
                <TitleRow
                  title={
                    <span className="block font-mono text-mono break-all text-ink">
                      {finding.site}
                    </span>
                  }
                  badge={<WhereaboutsBadge whereabouts={whereabouts(finding)} i18n={i18n} />}
                />
                <p className="mt-1 text-caption text-ink-subtle">
                  {t("finding.confirmedOn", { date: day(finding.confirmed_on) })}
                </p>
                <p className="text-caption text-ink-muted">
                  {t(`finding.extent.${finding.extent}.label`)}
                </p>
                <a
                  href={`/app/originals/${original.id}/copies/${finding.page_id}`}
                  className="tap mt-2 rounded-sm text-button text-brand-ink underline"
                >
                  {t("finding.openCaseFile")}
                </a>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
