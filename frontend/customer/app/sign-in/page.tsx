/**
 * Customer sign-in — the one page outside the session gate (ADR 0023, ADR 0036).
 *
 * The shell and every word are rendered on the server; the form itself is an island, because a
 * password has to be posted without leaving the page and the answer has to become a sentence. It
 * gets the `signIn`, `common` and `notFound` words as props, so no resource file travels with it.
 *
 * Someone who is already signed in is sent to the overview — a bookmarked sign-in URL, or a second
 * tab that signed in meanwhile.
 */
import { redirect } from "next/navigation";

import { Shell } from "../../components/Shell";
import { SignInForm } from "../../components/SignInForm";
import { Veil } from "../../components/ui";
import { isSignedOut } from "../../lib/django";
import { requestI18n } from "../../lib/i18n";
import { readAccount } from "../../lib/reads";
import { slice } from "../../lib/translate";

export default async function SignInPage() {
  const { t, language, resource } = await requestI18n();

  let signedIn = false;
  try {
    await readAccount();
    signedIn = true;
  } catch (error) {
    if (!isSignedOut(error)) {
      // Django unreachable, or something else entirely. The form is still the right thing to
      // render: it is where "we could not reach the server" belongs.
      signedIn = false;
    }
  }
  if (signedIn) redirect("/");

  return (
    <Shell i18n={{ t, language }} username={null}>
      {/* One of the three places the system allows the Veil, and it sits behind the card only. */}
      {/* The negative margins undo `main`'s own padding, so they follow it: `p-4` here, `p-8` at `sm`. */}
      <div className="relative isolate -mx-4 -my-4 overflow-hidden px-4 py-8 sm:-mx-8 sm:-my-8 sm:px-8 sm:py-10">
        <Veil />
        <SignInForm language={language} words={slice(resource, "signIn", "common")} />
      </div>
    </Shell>
  );
}
