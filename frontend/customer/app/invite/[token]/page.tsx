/**
 * Redeeming an invite: `/app/invite/{token}` (issue #115, ADR 0052).
 *
 * The **second** page outside the session gate, and the only other one there will be: whoever
 * opens this link has no account yet, by definition. `lib/routes.test.ts` names both and would
 * fail on a third, which is the point.
 *
 * Everything that is not the form is rendered here, on the server: the step, the words, the
 * declaration's German wording and the documents' versions. The form itself is one island, because
 * a password has to be posted without leaving the page and a refusal has to become a sentence.
 *
 * Every reason the link does not work — unknown, expired, withdrawn, already used, the feature off
 * — arrives as Django's one identical 404 and is rendered as one identical page. Nothing here may
 * branch on which it was: this server does not know, and must not learn.
 */
import type { Metadata } from "next";

import { InviteScreen } from "../../../screens/InviteScreen";
import { isNotFound } from "../../../lib/django";
import { requestI18n } from "../../../lib/i18n";
import { readInvite } from "../../../lib/reads";
import { slice } from "../../../lib/translate";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  // A link that is forwarded or pasted must not become a search result.
  robots: { index: false, follow: false },
};

export default async function InvitePage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  const { t, language, resource } = await requestI18n();

  let state = null;
  try {
    state = await readInvite(token);
  } catch (error) {
    if (!isNotFound(error)) {
      // Django unreachable, or throttled. The screen says so; it never says the link is bad.
      state = null;
    }
  }

  return (
    <InviteScreen
      i18n={{ t, language }}
      token={token}
      state={state}
      words={slice(resource, "onboarding", "common", "signIn")}
    />
  );
}
