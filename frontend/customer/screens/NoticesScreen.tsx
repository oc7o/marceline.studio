/**
 * Every notice draft this account has prepared, across every original (`/app/notices`).
 *
 * The per-record screen (`/app/originals/{id}/notices`) only ever showed one original's drafts, so
 * an account with several originals had no way to see what it had sent without opening each one in
 * turn. This is the account-wide answer: newest first, each row linking back to the original it
 * belongs to and into the record's own drafts screen.
 */
import { type NoticeTone, includedFindingsCount, noticeTone } from "@shared/app/notices";

import { NoticeSubmissionsBlock } from "../components/NoticeSubmissionsBlock";
import { BADGE, LoadError, TITLE_ROW, TitleRow, formatDate } from "../components/parts";
import { DefinitionList, EmptyState, Field } from "../components/ui";
import type { Notice, NoticeSubmission } from "../lib/reads";
import type { Words } from "../lib/words";

const TONE_CLASS: Record<NoticeTone, string> = {
  neutral: "bg-surface-300 text-ink-muted",
  caution: "bg-caution-quiet text-caution-ink",
  positive: "bg-positive-quiet text-positive-ink",
  critical: "bg-critical-quiet text-critical-ink",
};

export function NoticesScreen({
  i18n,
  notices,
  submissions,
}: {
  i18n: Words;
  /** null when the read failed; the screen says so rather than showing an empty list. */
  notices: Notice[] | null;
  /** null when the read failed; the submissions block simply renders nothing then. */
  submissions: NoticeSubmission[] | null;
}) {
  const { t } = i18n;
  return (
    <section aria-labelledby="notices-list-heading" className="flex flex-col gap-3">
      <div>
        <h1 id="notices-list-heading" className="font-display text-display-m text-ink">
          {t("notices.list.heading")}
        </h1>
        <p className="mt-1 text-body-s text-ink-muted">{t("notices.list.lead")}</p>
      </div>
      <NoticeSubmissionsBlock i18n={i18n} submissions={submissions} refreshHref="/app/notices" />
      {notices === null ? (
        <LoadError i18n={i18n} href="/app/notices" />
      ) : notices.length === 0 ? (
        <EmptyState title={t("notices.list.empty")} next={t("notices.list.emptyNext")} />
      ) : (
        <ul className="flex flex-col gap-2">
          {notices.map((notice) => (
            <li key={notice.id}>
              <NoticeRow i18n={i18n} notice={notice} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function NoticeRow({ i18n, notice }: { i18n: Words; notice: Notice }) {
  const { t, language } = i18n;
  const copies = includedFindingsCount(notice);
  return (
    <article
      aria-label={`${notice.work_title} · ${t(`notices.statuses.${notice.status}`)}`}
      className="rounded-md border border-hairline bg-surface-100 p-4 text-ink"
    >
      <TitleRow
        title={
          <a
            href={`/app/originals/${notice.original_id}`}
            title={notice.work_title}
            className={`text-body font-medium text-brand-ink underline ${TITLE_ROW.clamp}`}
          >
            {notice.work_title}
          </a>
        }
        badge={
          <span
            data-status={notice.status}
            className={`${BADGE} ${TONE_CLASS[noticeTone(notice)]}`}
          >
            {t(`notices.statuses.${notice.status}`)}
          </span>
        }
      />
      <p className="mt-1 text-body-s text-ink-muted">{t("notices.list.copies", { count: copies })}</p>
      <DefinitionList>
        <Field label={t("notices.list.sentLabel")}>
          {notice.marked_sent_at
            ? formatDate(notice.marked_sent_at, language)
            : t("notices.list.notSentYet")}
        </Field>
      </DefinitionList>
      <a
        href={`/app/originals/${notice.original_id}/notices`}
        className="tap mt-2 text-body-s text-brand-ink underline"
      >
        {t("notices.list.open")}
      </a>
    </article>
  );
}
