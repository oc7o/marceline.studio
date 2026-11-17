/**
 * Showing us that the platform account is yours (ADR 0055) — `/app/verification`.
 *
 * Three rules shaped every sentence here:
 *
 * - **we never ask for a document.** No ID copy, no date of birth — the screen
 *   says so, because a creator who has been asked for one elsewhere will assume we will too;
 * - **a person looks, by hand.** Nothing here is automatic, nothing of ours opens the profile, and
 *   the words say "someone on our side looks at your public profile", never "we check the link";
 * - **the wording rules still hold** (`wording.test.ts`): no "verified", no "confirmed". A state is
 *   what our record holds — looked at, not looked at yet, withdrawn.
 *
 * Its own slice, like `onboarding`: only this screen shows any of it, and only the Next app has it.
 */

export const enVerification = {
  verification: {
    heading: "Show us the account is yours",
    lead: "You get a short code. Put it in the bio of the account whose work we watch for you, or send it to us from that account — then someone on our side opens your public profile and writes down what they saw.",
    noDocuments:
      "We never ask for an ID document or a date of birth, and we do not keep one.",
    notOnline: "This step is not switched on for your account yet. We will write to you when it is.",
    status: {
      heading: "Where this stands",
      unverified: "Nobody has looked yet",
      verified: "Our team has seen it",
      revoked: "Withdrawn by our team",
    },
    method: {
      heading: "How it was established",
      bio_code: "the code was in the account's public bio",
      dm_code: "the code reached us from the account",
      video_call: "a short video call with our team",
      staff_legacy: "recorded by our team outside this step",
    },
    claim: {
      heading: "Which account is yours?",
      platform: "Platform",
      platformPlaceholder: "Choose one",
      handle: "Your name on it",
      handleHelp: "Without the @, exactly as it is spelled there.",
      url: "Link to the profile (optional)",
      urlHelp:
        "Only an address on that platform itself. It stays text for us to open by hand — nothing of ours opens it.",
      videoCallOnly:
        "For this one a code would not tell us anything: nobody looked at who you are before you could post there. Our team will set up a short video call with you instead — still no document to send in, and we keep nothing from it but the date.",
      submit: "Get my code",
      submitting: "One moment …",
      again: "Get a new code",
      pending: "Received. Your code will show here shortly.",
    },
    code: {
      heading: "Your code",
      place:
        "Copy it into the bio of that account, or send it to us in a message from it. You can take it out again as soon as we have looked.",
      expires: "Usable until {{when}}",
      expired: "This code has run out. Ask for a new one above.",
      placed: "I have put it there",
      placing: "One moment …",
      waiting: "Thanks — someone will look at your profile shortly.",
      videoCall:
        "No code for this one. Our team will write to you to arrange a short video call.",
    },
    accounts: {
      heading: "Your accounts",
      empty: "No account claimed yet.",
      emptyNext: "Name one above and you get a code for it.",
      state: {
        pending: "Waiting for you to place the code",
        placed: "Waiting for us to look",
        verified: "Our team has seen it",
        rejected: "Our team did not find the code",
        expired: "The code ran out",
        revoked: "Withdrawn by our team",
      },
      rejectedNext: "Ask for a new code and place it again.",
      noLink: "No link given",
    },
    errors: {
      invalid_handle: "That does not look like a name on that platform. Leave the @ and any link out.",
      host_not_allowed: "That address is not on the platform you chose. Use the profile's own address.",
      url_not_accepted_for_platform:
        "For this choice we do not take a link. Tell us the address in your reply instead.",
      unknown_platform: "Choose one of the platforms in the list.",
      code_expired: "That code has run out. Ask for a new one.",
      not_open: "This account has already been dealt with. Name it again for a new code.",
      video_call_required:
        "This one is settled on a short video call, not with a code. Our team will write to you.",
      account_verification_unavailable: "This step is not switched on for your account yet.",
      generic: "That did not go through. Try again, and write to us if it keeps failing.",
    },
    next: {
      heading: "What this unlocks",
      body: "Once our team has seen it, your record no longer waits on us for that step, and you can prepare a notice for a copy of your work.",
      rights: "Your rights and works",
    },
  },
};

export type VerificationWords = typeof enVerification;
