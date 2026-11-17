/**
 * Every notice draft this account has prepared, across every original (`/app/notices`).
 *
 * The per-record screen (`/app/originals/$originalId/notices`) only ever showed one original's
 * drafts, so an account with several originals had no way to see what it had sent without opening
 * each one in turn. This is the account-wide answer: newest first, each row linking back to the
 * original it belongs to and to the record's own drafts screen.
 */
import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { type NoticeTone, includedFindingsCount, noticeTone } from "./notices";
import { NoticeSubmissionsBlock } from "./NoticeSubmissionsBlock";
import { BADGE, LoadError, Loading, TITLE_ROW, TitleRow, useFormatDate } from "./parts";
import { type Notice, useNotices } from "./queries";

const TONE_CLASS: Record<NoticeTone, string> = {
  neutral: "bg-slate-100 text-slate-700",
  caution: "bg-amber-100 text-amber-900",
  positive: "bg-emerald-100 text-emerald-900",
  critical: "bg-rose-100 text-rose-900",
};

export function NoticesScreen() {
  const { t } = useTranslation();
  const notices = useNotices();

  return (
    <section aria-labelledby="notices-list-heading" className="flex flex-col gap-3">
      <div>
        <h1 id="notices-list-heading" className="text-xl font-bold">
          {t("notices.list.heading")}
        </h1>
        <p className="mt-1 text-sm text-slate-600">{t("notices.list.lead")}</p>
      </div>
      <NoticeSubmissionsBlock />
      {notices.isPending ? (
        <Loading />
      ) : notices.isError ? (
        <LoadError onRetry={() => void notices.refetch()} />
      ) : notices.data.length === 0 ? (
        <p className="rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-600">
          {t("notices.list.empty")}
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {notices.data.map((notice) => (
            <li key={notice.id}>
              <NoticeRow notice={notice} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function NoticeRow({ notice }: { notice: Notice }) {
  const { t } = useTranslation();
  const formatDate = useFormatDate();
  const copies = includedFindingsCount(notice);

  return (
    <article
      aria-label={`${notice.work_title} · ${t(`notices.statuses.${notice.status}`)}`}
      className="rounded-lg border border-slate-200 bg-white p-3"
    >
      <TitleRow
        title={
          <Link
            to="/app/originals/$originalId"
            params={{ originalId: notice.original_id }}
            title={notice.work_title}
            className={`font-medium text-sky-800 underline ${TITLE_ROW.clamp}`}
          >
            {notice.work_title}
          </Link>
        }
        badge={
          <span data-status={notice.status} className={`${BADGE} ${TONE_CLASS[noticeTone(notice)]}`}>
            {t(`notices.statuses.${notice.status}`)}
          </span>
        }
      />
      <div className="text-sm text-slate-700">{t("notices.list.copies", { count: copies })}</div>
      <div className="text-xs text-slate-500">
        {notice.marked_sent_at
          ? t("notices.list.sentOn", { date: formatDate(notice.marked_sent_at) })
          : t("notices.list.notSentYet")}
      </div>
      <Link
        to="/app/originals/$originalId/notices"
        params={{ originalId: notice.original_id }}
        className="mt-2 inline-block text-sm font-medium text-sky-800 underline"
      >
        {t("notices.list.open")}
      </Link>
    </article>
  );
}
