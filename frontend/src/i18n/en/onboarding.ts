/**
 * The invite redemption's words (issue #115, ADR 0052) — the one screen a person sees before they
 * have an account.
 *
 * Two rules shaped almost every sentence here:
 *
 * - **the neutral page says nothing.** Expired, already used, withdrawn and never-existed share
 *   one text, because anything else would tell whoever holds the link whether an address was
 *   invited;
 * - **the account is honestly unfinished at the end.** A tick does not make somebody a business,
 *   so the last screen says the account is being looked at by
 *   a person, and what can already be done meanwhile — never that everything is settled.
 *
 * Its own slice: only `/app/invite/{token}` shows any of it, and a visitor who signs in normally
 * should not download a word of it.
 */

export const enOnboarding = {
  onboarding: {
    heading: "Set up your account",
    lead: "Four short steps. You can close this page and open the link again; it remembers where you were.",
    progress: "Step {{step}} of {{total}}",
    steps: {
      password: "Password",
      declaration: "Your statements",
      documents: "The documents",
      country: "Where you are",
    },
    unavailable: {
      heading: "This link cannot be opened",
      body: "Write to the person who sent it to you and ask for a new one.",
    },
    email: {
      label: "Your sign-in address",
      fixed: "This address was set with the invitation and cannot be changed here.",
    },
    password: {
      heading: "Choose a password",
      lead: "You sign in with the address above and this password.",
      label: "Password",
      requirement: "At least {{count}} characters, and not something easy to guess.",
      submit: "Continue",
      submitting: "Saving …",
      errors: {
        password_too_short: "That is too short. Use at least {{count}} characters.",
        password_too_common: "That password is too common. Please choose another one.",
        password_entirely_numeric: "Digits alone are too easy to guess. Add letters or words.",
        password_too_similar: "That is too close to your address. Please choose something else.",
        password_invalid: "That password was not accepted. Please choose another one.",
      },
    },
    declaration: {
      heading: "Your statements",
      lead: "Two sentences, and they are the ones you are signing. They are in German, which is the language of the agreement.",
      version: "Version {{version}}",
      tick: "I agree to this statement.",
      // The majority and legal-capacity declaration (issue #123). Its own words, so that ticking
      // the business sentence is never mistaken for ticking this one.
      ageHeading: "Your age",
      ageLead: "You must be of age. We ask you to say so; we ask for no date of birth and no identity document.",
      ageTick: "I agree to this statement.",
      ageRequired: "Please tick the statement about your age to continue.",
      submit: "Continue",
      submitting: "Saving …",
      required: "Please tick the statement about your work to continue.",
      outdated: "The wording has changed since this page was opened. Please reload it and read the new text.",
    },
    documents: {
      heading: "The documents",
      lead: "Please read both and agree to them. We record which version you were shown.",
      open: "Read (opens in a new tab)",
      version: "Version {{version}}",
      names: {
        pilot_agreement: "Pilot agreement",
        service_privacy_notice: "Privacy information for the service",
      },
      tick: "I have read both documents and agree to them.",
      submit: "Continue",
      submitting: "Saving …",
      allRequired: "Please agree to both documents to continue.",
      notPublished: "The documents are not ready yet, so this step cannot be finished today. Nothing you have entered is lost — open the link again in a few days, or write to the person who invited you.",
    },
    country: {
      heading: "Where is your business based?",
      lead: "The pilot is open in the European Union only.",
      label: "Country",
      choose: "Please choose",
      submit: "Finish",
      submitting: "Finishing …",
      required: "Please choose a country.",
      outside: {
        heading: "Not open where you are, yet",
        body: "The pilot runs in the European Union only, so we cannot set up an account for you today. Nothing has been created, and you can write to us if you think this is a mistake.",
      },
    },
    done: {
      heading: "Your account is ready",
      body: "You are signed in. Add your first work next — the file stays on your computer.",
      cta: "Add your first work",
      // ADR 0055: only shown where the step exists (`account_step_next`), so it never points at a
      // screen that is not there.
      accountStep:
        "One more short step after that: showing us that the account whose work we watch is yours. A code, a minute, and no document of any kind.",
      accountStepCta: "Show us the account is yours",
    },
    // The last step on the public host (ADR 0052 §9): the account is created on the machine that
    // holds the fingerprints, a minute or two later, so there is nothing to sign in to yet. It says
    // what has happened and what to do — never "done", and never "failed" either.
    pending: {
      heading: "Your account is being set up",
      body: "We have everything we need. Your account is being created now; please try signing in in a few minutes with the address above and the password you just chose.",
      cta: "Go to sign-in",
    },
    checking: {
      heading: "We are looking at your account",
      body: "One of us checks the details you gave before everything is switched on. You can already add your works in the meantime; we will write to you if anything is missing.",
    },
    errors: {
      generic: "That did not go through. Please try again.",
      formExpired: "The form has expired. Please reload the page and try again.",
      rateLimited: "Too many attempts. Please try again later.",
      businessStatus: "Your account is being set up by hand. Nothing is lost — we will write to you as soon as it is ready.",
      unavailable: "We cannot finish this right now. Nothing you entered is lost — please open the link again in a few minutes.",
    },
  },
};

export type OnboardingWords = typeof enOnboarding;
