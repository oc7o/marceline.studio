/**
 * Showing us that the platform account is yours (`/app/verification`, ADR 0055).
 *
 * A synchronous **server** component: the heading, the standing, the code and every claimed account
 * are HTML, because none of it needs a browser. One island sits inside it — `VerificationForm` — for
 * the two writes (claim an account, "I have placed it").
 *
 * What the words may not do here: promise a time, call anything "verified", or suggest that a link
 * we were given is opened by anything of ours. The screen also states, once and plainly, that we
 * never ask for an identity document or a date of birth — the one sentence a
 * creator who has been asked for one elsewhere needs to read first.
 */
import { VerificationForm } from "../components/VerificationForm";
import { formatDate } from "../components/parts";
import { Banner, EmptyState } from "../components/ui";
import type { ClaimedAccount, VerificationStatus } from "../lib/reads";
import { type Resource, slice } from "../lib/translate";
import type { Words } from "../lib/words";

/** States with a code the creator still has to do something with. */
const OPEN = new Set<ClaimedAccount["state"]>(["pending", "placed"]);

export function VerificationScreen({
  i18n,
  words,
  status,
}: {
  i18n: Words;
  /** The island's own words: the resource slice, never the whole resource (ADR 0036). */
  words: Resource;
  status: VerificationStatus;
}) {
  const { t, language } = i18n;
  const open = status.accounts.filter((account) => OPEN.has(account.state));
  return (
    <section aria-labelledby="verification-heading" className="flex flex-col gap-4">
      <div>
        <h1 id="verification-heading" className="font-display text-display-m text-ink">
          {t("verification.heading")}
        </h1>
        <p className="mt-1 max-w-[68ch] text-body text-ink-muted">{t("verification.lead")}</p>
      </div>
      {/* The promise about documents is standing context, not a status: a note. */}
      <Banner role="note">{t("verification.noDocuments")}</Banner>

      <div className="rounded-md border border-hairline bg-surface-100 p-4">
        <h2 className="text-label text-ink-muted">{t("verification.status.heading")}</h2>
        <p data-identity={status.identity_status} className="mt-1 text-body-l text-ink">
          {t(`verification.status.${status.identity_status}`)}
        </p>
        {status.method ? (
          <p className="mt-1 text-body-s text-ink-muted">
            {t("verification.method.heading")}: {t(`verification.method.${status.method}`)}
          </p>
        ) : null}
      </div>

      {/* The live code, server-rendered: reading it needs no JavaScript at all. A claim on a
          platform that runs no identity check of its own has **no code** (ADR 0055, operator review
          2026-09-19) — it says so instead of printing a dash. */}
      {open.map((account) => (
        <div key={account.id} className="rounded-md border border-hairline bg-surface-100 p-4">
          <h2 className="text-label text-ink-muted">{t("verification.code.heading")}</h2>
          {account.code === null ? (
            <p className="mt-1 max-w-[68ch] text-body-s text-ink">
              {t("verification.code.videoCall")}
            </p>
          ) : (
            <>
              <p className="mt-1 font-mono text-mono break-all text-ink select-all">
                {account.code}
              </p>
              <p className="mt-2 max-w-[68ch] text-body-s text-ink-muted">
                {t("verification.code.place")}
              </p>
            </>
          )}
          {account.code_expires_at ? (
            <p className="mt-1 text-caption text-ink-subtle">
              {t("verification.code.expires", {
                when: formatDate(account.code_expires_at, language),
              })}
            </p>
          ) : null}
          {account.state === "placed" ? (
            <p className="mt-2 text-body-s text-ink">{t("verification.code.waiting")}</p>
          ) : null}
        </div>
      ))}

      <VerificationForm language={language} words={words} status={status} />

      <div className="flex flex-col gap-2">
        <h2 className="text-label text-ink-muted">{t("verification.accounts.heading")}</h2>
        {status.accounts.length === 0 ? (
          <EmptyState
            title={t("verification.accounts.empty")}
            next={t("verification.accounts.emptyNext")}
          />
        ) : (
          <ul className="flex flex-col gap-2">
            {status.accounts.map((account) => (
              <li key={account.id}>
                <ClaimedAccountEntry i18n={i18n} account={account} />
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="rounded-md border border-hairline bg-surface-100 p-4">
        <h2 className="text-label text-ink-muted">{t("verification.next.heading")}</h2>
        <p className="mt-1 max-w-[68ch] text-body-s text-ink-muted">{t("verification.next.body")}</p>
        <a href="/app/rights" className="tap mt-2 inline-block rounded-sm text-button text-brand-ink underline">
          {t("verification.next.rights")}
        </a>
      </div>
    </section>
  );
}

function ClaimedAccountEntry({ i18n, account }: { i18n: Words; account: ClaimedAccount }) {
  const { t } = i18n;
  return (
    <article
      aria-label={account.handle}
      data-state={account.state}
      className="rounded-none border border-hairline bg-surface-100 p-3"
    >
      <div className="text-body-l font-medium text-ink">{account.handle}</div>
      {/* The address the creator gave us, as text. Never a link: it is written by somebody else,
          and one mis-click in a customer screen would be a request we promised not to make. */}
      <div className="font-mono text-mono-s break-all text-ink-subtle">
        {account.profile_url ?? t("verification.accounts.noLink")}
      </div>
      <p className="mt-2 text-body-s text-ink">{t(`verification.accounts.state.${account.state}`)}</p>
      {account.state === "rejected" ? (
        <p className="mt-1 text-body-s text-ink-muted">{t("verification.accounts.rejectedNext")}</p>
      ) : null}
      {account.state === "expired" ? (
        <p className="mt-1 text-body-s text-ink-muted">{t("verification.code.expired")}</p>
      ) : null}
    </article>
  );
}

/** The slice this screen's island needs, in one place so the page cannot pass the wrong one. */
export function verificationWords(resource: Resource): Resource {
  return slice(resource, "verification", "common");
}
