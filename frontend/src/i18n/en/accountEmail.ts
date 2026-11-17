/**
 * The words of the two mailed account actions (issue #149, ADR 0054): a password reset and an
 * address confirmation.
 *
 * Two rules shaped almost every sentence:
 *
 * - **the request says nothing about the address.** "If this address has an account, a mail is on
 *   its way" is true for an address that has none, and that is exactly why it is worded that way;
 * - **two endings, both honest.** On the public host the new password is accepted and set a minute
 *   later (the account lives on the machine that holds the fingerprints), so there is a second
 *   sentence for that — and neither ending signs anybody in, because a mailed link proves the reader
 *   has the inbox, not that they know the password they just chose.
 *
 * **It never says "confirm".** `wording.test.ts` bans that word and the German participle
 * `bestätigt`, because they are our review verdict about a finding and an address
 * flow does not get to borrow it. These screens say what actually happens instead — we learn that the
 * address reaches the person — which is plainer anyway. German keeps the ordinary verb, which the
 * ban's pattern leaves alone.
 *
 * Its own slice: only these four screens show any of it, and a visitor who signs in normally should
 * not download a word of it. It is **not** in `src/i18n/slices.ts` — the SPA has no such screen.
 */

export const enAccountEmail = {
  accountEmail: {
    account: "Account",
    backToSignIn: "Back to sign-in",
    unavailable: {
      // It names **no reason**: this server is not told which of them it was, and a guess here would
      // be the oracle the one 404 exists to prevent. My own screens test asserts the absence.
      heading: "This link cannot be opened",
      body: "Ask for a new one from the sign-in page.",
    },
    forgot: {
      heading: "Set a new password",
      lead: "Give us the address you sign in with and we will send you a link.",
      label: "Email address",
      submit: "Send the link",
      submitting: "Sending …",
    },
    sent: {
      heading: "Check your inbox",
      body: "If this address has an account, a mail with a link is on its way. The link works once.",
    },
    reset: {
      heading: "Set a new password",
      lead: "Choose a new password for this account. The link you opened works only once.",
      label: "New password",
      requirement: "At least {{count}} characters, and not something easy to guess.",
      submit: "Save the password",
      submitting: "Saving …",
      done: "Your password is set",
      doneBody: "You can sign in with it now.",
      accepted: "Your password is being set",
      acceptedBody: "Please sign in in a few minutes with the address above and the password you just chose.",
    },
    confirm: {
      heading: "Is this your address?",
      lead: "One press, and we know this address reaches you.",
      submit: "Yes, this is my address",
      submitting: "Saving …",
      done: "Thank you",
      doneBody: "We know this address reaches you now. You can close this page.",
    },
    resend: {
      heading: "Your email address",
      lead: "We asked whether the address this account signs in with reaches you. You can use the account either way — it only means we can reach you reliably.",
      submit: "Send the mail again",
      submitting: "Sending …",
      done: "The mail is on its way",
      doneBody: "Open the link in it, and we will know the address reaches you.",
      unavailable: "We cannot send it right now. Write to us and a person will look at it.",
    },
    password: {
      errors: {
        password_too_short: "That is too short. Use at least {{count}} characters.",
        password_too_common: "That password is too common. Please choose another one.",
        password_entirely_numeric: "Digits alone are too easy to guess. Add letters or words.",
        password_too_similar: "That is too close to your address. Please choose something else.",
        password_invalid: "That password was not accepted. Please choose another one.",
      },
    },
    errors: {
      generic: "That did not go through. Please try again.",
      formExpired: "The form has expired. Please reload the page and try again.",
      rateLimited: "Too many attempts. Please try again later.",
      unavailable: "We cannot do that right now. Please try again in a few minutes.",
      linkGone: "This link cannot be used any more. Ask for a new one from the sign-in page.",
    },
  },
};

export type AccountEmailWords = typeof enAccountEmail;
