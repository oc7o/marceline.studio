/**
 * `/app/originals/{orig_…}/notices` — "prepare notice draft" (issue #33, ADR 0035; ported ADR 0036).
 *
 * The candidates and this record's drafts are read on the server; the form below them is one
 * island, because selecting pages, picking a recipient and ticking confirmations are interaction
 * from the first click. A 404 is the not-found page, as it is everywhere else: an unknown original
 * and another organisation's read alike (ADR 0028 c), and so does a customer API that does not
 * serve notices yet.
 */
import { notFound } from "next/navigation";

import { parsePublicId } from "@shared/app/caseFile";

import { NoticeDraftScreen } from "../../../../../components/NoticeDraftScreen";
import { NoticeSubmissionsBlock } from "../../../../../components/NoticeSubmissionsBlock";
import { LoadError } from "../../../../../components/parts";
import { requestI18n } from "../../../../../lib/i18n";
import { found, optional } from "../../../../../lib/pageReads";
import {
  type NoticeCandidates,
  readAccount,
  readNoticeCandidates,
  readNoticeSubmissions,
  readNotices,
} from "../../../../../lib/reads";
import { slice } from "../../../../../lib/translate";

export default async function NoticesPage({
  params,
}: {
  params: Promise<{ originalId: string }>;
}) {
  const { originalId } = await params;
  const id = parsePublicId("orig", originalId);
  if (id === null) notFound();

  const { t, language, resource } = await requestI18n();
  const account = await optional(readAccount);
  if (account === null) return null;

  const candidates: NoticeCandidates | null = await found(() => readNoticeCandidates(id));
  if (candidates === null) {
    return (
      <section className="flex flex-col gap-3">
        <a href={`/app/originals/${id}`} className="tap rounded-sm text-body-s text-brand-ink underline">
          {t("original.back")}
        </a>
        <LoadError i18n={{ t, language }} href={`/app/originals/${id}/notices`} />
      </section>
    );
  }

  const all = (await optional(readNotices)) ?? [];
  const submissions = await optional(readNoticeSubmissions);
  return (
    <div className="flex flex-col gap-4">
      <NoticeSubmissionsBlock
        i18n={{ t, language }}
        submissions={submissions}
        refreshHref={`/app/originals/${id}/notices`}
      />
      <NoticeDraftScreen
        originalId={id}
        language={language}
        words={slice(resource, "notices", "original", "common")}
        candidates={candidates}
        notices={all.filter((notice) => notice.original_id === id)}
        organisationName={account.organisation?.name ?? ""}
      />
    </div>
  );
}
