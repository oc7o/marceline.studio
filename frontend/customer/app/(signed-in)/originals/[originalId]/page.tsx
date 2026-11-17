/**
 * `/app/originals/{orig_…}` — one original.
 *
 * The route parameter goes through `parsePublicId` before any request: a public id is `orig_` plus
 * 32 hex digits (ADR 0033), and anything else — an old integer URL included — is not found without
 * asking, which is the same answer the API would give.
 */
import { notFound } from "next/navigation";

import { parsePublicId } from "@shared/app/caseFile";

import { requestI18n } from "../../../../lib/i18n";
import { found } from "../../../../lib/pageReads";
import { readCase } from "../../../../lib/reads";
import { LoadError } from "../../../../components/parts";
import { OriginalScreen } from "../../../../screens/OriginalScreen";

export default async function OriginalPage({
  params,
}: {
  params: Promise<{ originalId: string }>;
}) {
  const { originalId } = await params;
  const id = parsePublicId("orig", originalId);
  if (id === null) notFound();
  const { t, language } = await requestI18n();
  const detail = await found(() => readCase(id));
  if (detail === null) return <LoadError i18n={{ t, language }} href={`/app/originals/${id}`} />;
  return <OriginalScreen i18n={{ t, language }} detail={detail} />;
}
