/**
 * The one not-found page (ADR 0028 c's 404 rule).
 *
 * An unknown original, another organisation's original, a copy that is not among an original's
 * findings and a typed URL all land here and read identically — that is the point: a customer must
 * not be able to tell a safety-hidden page or someone else's record from a typo.
 */
import { Shell } from "../components/Shell";
import { isSignedOut } from "../lib/django";
import { requestI18n } from "../lib/i18n";
import { readAccount } from "../lib/reads";

export default async function NotFoundPage() {
  const { t, language } = await requestI18n();
  let username: string | null = null;
  try {
    username = (await readAccount()).username;
  } catch (error) {
    // Signed out, or the server is unreachable: either way this page is text and shows the shell
    // without navigation rather than redirecting a 404 somewhere else.
    if (!isSignedOut(error)) username = null;
  }
  return (
    <Shell i18n={{ t, language }} username={username}>
      <section className="rounded-md border border-hairline bg-surface-100 p-6">
        <h1 className="font-display text-display-m text-ink">{t("notFound.heading")}</h1>
        <p className="mt-2 max-w-[68ch] text-body text-ink-muted">{t("notFound.original")}</p>
        <a href="/app" className="tap mt-4 rounded-sm text-button text-brand-ink underline">
          {t("notFound.back")}
        </a>
      </section>
    </Shell>
  );
}
