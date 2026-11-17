/**
 * Recent notice writes still pending, or refused after acceptance (issue #86, ADR 0046 §7).
 *
 * On production a notice write (prepare / approve / mark sent / response / withdraw / correct) is
 * accepted first (202) and applied on development shortly after; only then can it be refused, for
 * a reason only development can judge (the rights gate, the draft's state, whether the page is
 * still there). `GET /app/notices/submissions` is how the customer learns the outcome, and this is
 * the one place it renders: on `/app/notices` and on `/app/originals/{id}/notices`, since a write
 * belongs to no single draft before it has one. A write that went through needs no line — the
 * draft itself, open on this same list, is the record of success — so `isUnsettled` is what keeps
 * this list to what is still pending or was refused.
 */
import { useTranslation } from "react-i18next";

import { type NoticeSubmission, isUnsettled, submissionKindKey, submissionMessage } from "./notices";
import { useFormatDate } from "./parts";
import { useNoticeSubmissions } from "./queries";

/** `done` is never shown (`isUnsettled` filters it out before rendering) but is typed anyway: the
 * status is the API's own type, not a narrowed one — the same reasoning as `SUBMISSION_BADGE`. */
const STATUS_BADGE: Record<NoticeSubmission["status"], string> = {
  processing: "inline-block rounded bg-sky-100 px-2 py-0.5 text-xs font-semibold text-sky-900",
  done: "inline-block rounded bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-900",
  refused: "inline-block rounded bg-slate-200 px-2 py-0.5 text-xs font-semibold text-slate-800",
  failed: "inline-block rounded bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900",
};

export function NoticeSubmissionsBlock() {
  const { t } = useTranslation();
  const formatDate = useFormatDate();
  const submissions = useNoticeSubmissions();
  const open: NoticeSubmission[] = (submissions.data ?? []).filter(isUnsettled);
  if (open.length === 0) return null;
  return (
    <section
      aria-labelledby="notice-submissions-heading"
      className="rounded-lg border border-slate-200 bg-white p-3 text-sm text-slate-700"
    >
      <h2 id="notice-submissions-heading" className="font-semibold">
        {t("notices.submissions.heading")}
      </h2>
      <p className="mt-1 text-xs text-slate-500">{t("notices.submissions.lead")}</p>
      <ul className="mt-2 flex flex-col gap-2">
        {open.map((submission) => (
          <li key={submission.id}>
            <p className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{t(submissionKindKey(submission.kind))}</span>
              <span className={STATUS_BADGE[submission.status]}>
                {t(`notices.submissions.status.${submission.status}`)}
              </span>
            </p>
            <p role={submission.status === "processing" ? "status" : undefined} className="text-slate-600">
              {t(submissionMessage(submission))}
            </p>
            <p className="mt-0.5 text-xs text-slate-500">{formatDate(submission.received_at)}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}
