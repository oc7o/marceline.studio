/**
 * Recent notice writes still pending, or refused after acceptance (issue #86, ADR 0046 §7).
 *
 * On production a notice write (prepare / approve / mark sent / response / withdraw / correct) is
 * accepted first (202) and applied on development shortly after; only then can it be refused, for
 * a reason only development can judge (the rights gate, the draft's state, whether the page is
 * still there). `GET /app/notices/submissions` is how the customer learns the outcome, and this is
 * the one place it renders: on `/app/notices` and on `/app/originals/{id}/notices`, since a write
 * belongs to no single draft before it has one.
 *
 * A plain **server** component, like every screen in this app: it costs the route nothing beyond
 * its own words, and needs no client JavaScript to stay current. Nothing here polls — `refreshHref`
 * is a plain, no-JS link back to the same page, offered only while something is still processing;
 * the customer's own next visit, or that one click, is what shows the settled state. A write that
 * went through needs no line here at all — the draft itself, open on this same list, is the record
 * of success — so `isUnsettled` is what keeps this list to what is still pending or was refused.
 */
import { type NoticeSubmission, isUnsettled, submissionKindKey, submissionMessage } from "@shared/app/notices";

import { NOTICE_SUBMISSION_TONES } from "../lib/tones";
import type { Words } from "../lib/words";
import { BADGE, formatDate } from "./parts";

export function NoticeSubmissionsBlock({
  i18n,
  submissions,
  refreshHref,
}: {
  i18n: Words;
  /** null when the read failed; the block simply renders nothing rather than an empty section. */
  submissions: NoticeSubmission[] | null;
  /** Where the "reload" hint points: the same page, plain — no client navigation needed. */
  refreshHref: string;
}) {
  const { t, language } = i18n;
  const open = (submissions ?? []).filter(isUnsettled);
  if (open.length === 0) return null;
  const processing = open.some((submission) => submission.status === "processing");
  return (
    <section
      aria-labelledby="notice-submissions-heading"
      className="rounded-md border border-hairline bg-surface-100 p-4 text-body-s text-ink"
    >
      <h2 id="notice-submissions-heading" className="font-display text-display-s text-ink">
        {t("notices.submissions.heading")}
      </h2>
      <p className="mt-1 text-ink-muted">{t("notices.submissions.lead")}</p>
      <ul className="mt-2 flex flex-col gap-2">
        {open.map((submission) => (
          <li key={submission.id}>
            <p className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-ink">{t(submissionKindKey(submission.kind))}</span>
              <span className={`${BADGE} ${NOTICE_SUBMISSION_TONES[submission.status]}`}>
                {t(`notices.submissions.status.${submission.status}`)}
              </span>
            </p>
            <p role={submission.status === "processing" ? "status" : undefined} className="text-ink-muted">
              {t(submissionMessage(submission))}
            </p>
            <p className="mt-0.5 text-body-s text-ink-subtle">
              {formatDate(submission.received_at, language)}
            </p>
          </li>
        ))}
      </ul>
      {processing ? (
        <a href={refreshHref} className="mt-2 inline-block text-body-s text-brand-ink underline">
          {t("notices.submissions.refreshHint")}
        </a>
      ) : null}
    </section>
  );
}
