/**
 * `/app/export` (issue #131) — where `GET /app/api/export` redirects when the download did not
 * happen, instead of relaying the refusal as a file. `problem` is Django's status, already
 * reduced to one of three words by the route handler; nothing here talks to Django again.
 */
import { requestI18n } from "../../../lib/i18n";
import { ExportResultScreen, exportProblemFromParam } from "../../../screens/ExportResultScreen";

export default async function ExportResultPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const { t, language } = await requestI18n();
  const { problem } = await searchParams;
  return <ExportResultScreen i18n={{ t, language }} problem={exportProblemFromParam(problem)} />;
}
