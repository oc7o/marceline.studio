/**
 * One original (`/app/originals/$originalId`): the state of its case and every copy found of it,
 * each linking to its own case file.
 *
 * Reads `GET /api/v1/app/cases/{id}`, the same response the case file uses, so moving between the
 * two costs no second request. A 404 — unknown original or someone else's, deliberately the same —
 * renders "not found" and nothing else.
 */
import { Link, useParams } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { parsePublicId, whereabouts } from "./caseFile";
import {
  Card,
  LoadError,
  Loading,
  NotFound,
  StateBadge,
  TitleRow,
  WhereaboutsBadge,
  isNotFound,
  useFormatDate,
  useFormatDay,
} from "./parts";
import { type CaseDetail, useCustomerCase } from "./queries";

export function OriginalScreen() {
  const { originalId } = useParams({ from: "/app/originals/$originalId" });
  const id = parsePublicId("orig", originalId);
  if (id === null) return <NotFound what="original" />;
  return <OriginalLoader originalId={id} />;
}

function OriginalLoader({ originalId }: { originalId: string }) {
  const detail = useCustomerCase(originalId);
  if (detail.isPending) return <Loading />;
  if (detail.isError) {
    return isNotFound(detail.error) ? <NotFound what="original" /> : <LoadError onRetry={() => void detail.refetch()} />;
  }
  return <OriginalView detail={detail.data} />;
}

function OriginalView({ detail }: { detail: CaseDetail }) {
  const { t } = useTranslation();
  const formatDate = useFormatDate();
  const formatDay = useFormatDay();
  const { original, findings } = detail;
  const caseRow = detail.case;

  return (
    <div className="flex flex-col gap-5">
      <div>
        <Link to="/app" className="text-sm text-sky-800 underline">
          {t("original.back")}
        </Link>
        {/* The detail heading is the one place the whole title shows, so it wraps instead of clamping. */}
        <div className="mt-2">
          <TitleRow
            title={<h1 className="text-xl font-bold break-words">{original.title}</h1>}
            badge={<StateBadge state={original.state} />}
          />
        </div>
        <p className="mt-1 text-sm text-slate-600">{t("original.added", { date: formatDay(original.created_at.slice(0, 10)) })}</p>
      </div>

      <Card labelledBy="case-heading">
        <h2 id="case-heading" className="font-semibold">
          {t("original.case.heading")}
        </h2>
        {caseRow ? (
          <div className="mt-1 flex flex-col gap-1 text-sm text-slate-700">
            <p>{t("original.case.opened", { date: formatDay(caseRow.opened_on) })}</p>
            {caseRow.state === "removed_watching" && caseRow.removed_at ? (
              <p>{t("original.case.removedWatching", { date: formatDate(caseRow.removed_at) })}</p>
            ) : (
              <p>{t("original.case.open")}</p>
            )}
            {caseRow.reopen_count > 0 && caseRow.reopened_on ? (
              <p className="text-amber-900">
                {t("original.case.reopened", { count: caseRow.reopen_count, date: formatDay(caseRow.reopened_on) })}
              </p>
            ) : null}
          </div>
        ) : (
          <p className="mt-1 text-sm text-slate-700">{t("original.case.none")}</p>
        )}
      </Card>

      {findings.length > 0 ? (
        <section aria-labelledby="findings-heading" className="flex flex-col gap-3">
          <div>
            <h2 id="findings-heading" className="font-semibold">
              {t("original.findings.heading")}
            </h2>
            <p className="text-sm text-slate-600">{t("original.findings.lead")}</p>
          </div>
          <ul className="flex flex-col gap-2">
            {findings.map((finding) => (
              <li key={finding.page_id} className="rounded-lg border border-slate-200 bg-white p-3">
                <TitleRow
                  title={<span className="block font-medium break-all">{finding.site}</span>}
                  badge={<WhereaboutsBadge whereabouts={whereabouts(finding)} />}
                />
                <p className="mt-1 text-xs text-slate-500">
                  {t("finding.confirmedOn", { date: formatDay(finding.confirmed_on) })}
                </p>
                <p className="text-xs text-slate-600">{t(`finding.extent.${finding.extent}.label`)}</p>
                <Link
                  to="/app/originals/$originalId/copies/$pageId"
                  params={{ originalId: original.id, pageId: finding.page_id }}
                  className="mt-2 inline-block text-sm font-medium text-sky-800 underline"
                >
                  {t("finding.openCaseFile")}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
