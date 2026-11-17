/** `/app/notices` — every notice draft this account has prepared, across all its originals. */
import { requestI18n } from "../../../lib/i18n";
import { optional } from "../../../lib/pageReads";
import { readNoticeSubmissions, readNotices } from "../../../lib/reads";
import { NoticesScreen } from "../../../screens/NoticesScreen";

export default async function NoticesListPage() {
  const { t, language } = await requestI18n();
  const [notices, submissions] = await Promise.all([
    optional(readNotices),
    optional(readNoticeSubmissions),
  ]);
  return (
    <NoticesScreen i18n={{ t, language }} notices={notices} submissions={submissions} />
  );
}
