/**
 * The invite redemption screen (issue #115, ADR 0052): a synchronous server component.
 *
 * Data in as props, HTML out — so `screens.test.tsx` renders it to a string without a request, and
 * so nothing here can quietly become a client component. The one island is the form.
 *
 * `state === null` is the **neutral page**: unknown, expired, withdrawn, already used, and the
 * feature switched off all arrive here as the same null and are rendered as the same paragraph.
 * There is no branch to add, because this server is not told which of them it was (ADR 0052 §6).
 */
import type { InviteState } from "../lib/reads";
import type { Resource } from "../lib/translate";
import type { Words } from "../lib/words";
import { InviteForm } from "../components/InviteForm";
import { Shell } from "../components/Shell";

const STEPS = ["password", "declaration", "documents", "country"] as const;

export function InviteScreen({
  i18n,
  token,
  state,
  words,
}: {
  i18n: Words;
  token: string;
  state: InviteState | null;
  words: Resource;
}) {
  if (state === null) {
    return (
      <Shell i18n={i18n} username={null}>
        <div className="mx-auto w-full max-w-md rounded-md border border-hairline bg-surface-100 p-6 shadow-md">
          <h1 className="font-display text-display-m text-ink">
            {i18n.t("onboarding.unavailable.heading")}
          </h1>
          <p className="mt-3 text-body text-ink-muted">{i18n.t("onboarding.unavailable.body")}</p>
        </div>
      </Shell>
    );
  }

  const position = STEPS.indexOf(state.step as (typeof STEPS)[number]);
  return (
    <Shell i18n={i18n} username={null}>
      <div className="mx-auto w-full max-w-md">
        <h1 className="font-display text-display-m text-ink">{i18n.t("onboarding.heading")}</h1>
        <p className="mt-2 text-body text-ink-muted">{i18n.t("onboarding.lead")}</p>
        {position < 0 ? null : (
          <p className="mt-4 text-label text-ink-subtle">
            {i18n.t("onboarding.progress", { step: position + 1, total: STEPS.length })} ·{" "}
            {i18n.t(`onboarding.steps.${state.step}`)}
          </p>
        )}
        <div className="mt-4 rounded-md border border-hairline bg-surface-100 p-6 shadow-md">
          <p className="text-label text-ink-subtle">{i18n.t("onboarding.email.label")}</p>
          <p className="text-body text-ink">{state.email}</p>
          <p className="mt-1 text-caption text-ink-subtle">{i18n.t("onboarding.email.fixed")}</p>
          <hr className="my-5 border-hairline" />
          <InviteForm language={i18n.language} token={token} state={state} words={words} />
        </div>
      </div>
    </Shell>
  );
}
