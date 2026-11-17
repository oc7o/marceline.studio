/**
 * `/app/originals/{orig_…}/copies/{page_…}` — one copy's record.
 *
 * A page that is not among this original's findings — including one hidden by the safety gate,
 * which the API never sends — is simply not found. There is no other code path to it, and a typo
 * looks exactly the same.
 */
import { notFound } from "next/navigation";

import { parsePublicId } from "@shared/app/caseFile";

import { requestI18n } from "../../../../../../lib/i18n";
import { found } from "../../../../../../lib/pageReads";
import { readCase } from "../../../../../../lib/reads";
import { LoadError } from "../../../../../../components/parts";
import { CaseFileScreen } from "../../../../../../screens/CaseFileScreen";

export default async function CaseFilePage({
  params,
}: {
  params: Promise<{ originalId: string; pageId: string }>;
}) {
  const { originalId, pageId } = await params;
  const original = parsePublicId("orig", originalId);
  const page = parsePublicId("page", pageId);
  if (original === null || page === null) notFound();

  const { t, language } = await requestI18n();
  const detail = await found(() => readCase(original));
  if (detail === null) {
    return (
      <LoadError i18n={{ t, language }} href={`/app/originals/${original}/copies/${page}`} />
    );
  }
  const finding = detail.findings.find((row) => row.page_id === page);
  if (!finding) notFound();

  return <CaseFileScreen i18n={{ t, language }} detail={detail} finding={finding} />;
}
