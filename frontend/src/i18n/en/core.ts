/**
 * The customer words every `/app` visit needs: the shell, the sign-in form, the overview and the
 * screens whose words are small enough that splitting them would save nothing.
 *
 * **This is the slice the first load pays for** (`frontend/scripts/check-bundle-budget.mjs`), so a
 * screen's own words belong in that screen's slice beside this file, not here. What is here on
 * purpose, although it belongs to a bigger screen's section, is the handful of keys the shell and
 * the overview name themselves — `caseFile.where.open` (the shared `PageAddress` part),
 * `addOriginal.submissions` (the overview's list of uploads being added) and
 * `rights.works.missing` / `.blockers` (the overview's rights warning). `en.ts` merges each of
 * them back into its section, so the key paths are exactly what they always were.
 */

export const enCore = {
  language: {
    group: "Language",
    de: "Deutsch",
    en: "English",
  },
  common: {
    loading: "Loading…",
    unreachable: "We cannot reach the server. Please check your connection and try again.",
    loadFailed: "This could not be loaded. Please try again in a moment.",
    retry: "Try again",
  },
  nav: {
    label: "Your account",
    overview: "Your originals",
    removals: "Removals log",
    notices: "Notices",
    invoices: "Invoices",
    rights: "Rights",
    /**
     * The same five places, named for the bottom bar a phone gets (issue #117). A fifth of a
     * 360 px screen is 72 px: the long names do not fit there, and a name that does not fit
     * either wraps into something unreadable or pushes the page sideways.
     */
    short: {
      overview: "Originals",
      removals: "Removals",
      notices: "Notices",
      invoices: "Invoices",
      rights: "Rights",
    },
  },
  coverage: {
    sources: {
      fewer_than_10: "fewer than 10 sources",
      "10_plus": "10+ sources",
      "50_plus": "50+ sources",
      "100_plus": "100+ sources",
      "250_plus": "250+ sources",
      "500_plus": "500+ sources",
      "1000_plus": "1,000+ sources",
    },
    watching: "We watch {{sources}} for copies of your work. We do not name them.",
    removals: "We currently watch {{sources}}.",
  },
  states: {
    nothing_found: "Nothing found",
    found_online: "Found · online",
    found_removed: "Found · removed",
  },
  // Server-side pagination over a list already read in full (issue #134); used by the overview's
  // originals list and the removals log, both core screens, so this lives here rather than a slice.
  pagination: {
    label: "Pages",
    status: "{{start}}–{{end}} of {{total}}",
    previous: "Back",
    next: "More",
  },
  overview: {
    heading: "Your originals",
    lead: "For each original: whether we found matching copies, where they are, and what has happened since.",
    summary: {
      found_online_one: "{{count}} with copies online",
      found_online_other: "{{count}} with copies online",
      found_removed_one: "{{count}} with every copy removed",
      found_removed_other: "{{count}} with every copy removed",
      nothing_found_one: "{{count}} with nothing found",
      nothing_found_other: "{{count}} with nothing found",
    },
    empty: "There are no originals in your account yet. We add them for you — write to us to get started.",
    /**
     * The first screen a new creator sees is empty, and "nothing here" on its own reads as a
     * product that is not doing anything (issue #117). One sentence for what the empty screen is
     * waiting for — and no time in it, because we do not know one.
     */
    emptyNext: "What happens next: an original is added, we compare it with every source we watch, and each matching copy a person has reviewed appears here with the site, the address and the dates.",
    confirmed_one: "{{count}} human-reviewed matching copy, {{removed}} of them removed",
    confirmed_other: "{{count}} human-reviewed matching copies, {{removed}} of them removed",
    sites: "On: {{sites}}",
    sitesFailed: "Where they are could not be loaded.",
    nothingFound: "No human-reviewed matching copy on the sites we watch.",
    openCase: "What has happened",
    monitoring: "Monitoring active",
    explainer: "We compare your originals with all monitored sources; matching copies reviewed by a person appear here.",
    addOriginal: "Add an original",
    emptyCanAdd: "There are no originals in your account yet. Add your first one — your files stay on your computer.",
    totals: {
      heading: "Your catalogue at a glance",
      originals_one: "{{count}} original",
      originals_other: "{{count}} originals",
    },
    recent: {
      heading: "Recent changes",
      empty: "Nothing has changed recently.",
      confirmed: "{{title}}: match reviewed by a person on {{date}}",
      removed: "{{title}}: removed on {{date}}",
      reappeared: "{{title}}: back online since {{date}}",
    },
    rightsLink: "Manage rights status",
  },
  notFound: {
    heading: "Not found",
    original: "There is no such original in your account.",
    caseFile: "There is no such record in your account.",
    back: "Back to your originals",
  },
  finding: {
    whereabouts: {
      online: "Online",
      gone: "Gone",
      unknown: "Status unknown",
    },
    confirmedOn: "Match reviewed by a person on {{date}}",
    openCaseFile: "Open the record",
    extent: {
      label: "Extent",
      full_copy: {
        label: "Full copy",
        explanation: "Every part of your video we compared was on this page.",
      },
      excerpt: {
        label: "Excerpt",
        explanation: "Only part of your video is on this page.",
      },
      not_determined: {
        label: "Extent not determined",
        explanation: "The preview images do not show how much of your video is on this page.",
        // A photo set has no timeline and no duration, so `copy_extent` classifies every photo
        // match `unknown` — this is the only extent a collection ever shows (ADR 0053 §5).
        explanationPhotos: "We do not determine how much of your photo set is on this page.",
      },
    },
  },
  original: {
    back: "All originals",
    added: "Added on {{date}}",
    case: {
      heading: "The record",
      none: "Nothing found on the sites we watch, so there is no record yet.",
      opened: "Opened on {{date}}.",
      open: "Open: not everything we found is gone.",
      removedWatching: "Everything we found has been gone since {{date}}. The record stays open, because copies are often uploaded again.",
      reopened_one: "Reopened {{count}} time after everything was gone, most recently on {{date}}.",
      reopened_other: "Reopened {{count}} times after everything was gone, most recently on {{date}}.",
    },
    findings: {
      heading: "Matching copies found",
      lead: "Each matching copy has its own record: where it is, why it matches your reference file, and which options exist.",
    },
  },
  removals: {
    heading: "Removals log",
    lead: "Every human-reviewed matching copy we later saw disappear, newest first. An entry reopens if the copy comes back.",
    watched: "“No copy online” means this copy is gone and no other matching copy of the same original is online on the sites we watch. We can only speak for the sites we watch: a copy somewhere else would not show up here.",
    empty: "Nothing has been removed yet.",
    emptyNext: "What happens next: when a matching copy listed in your account disappears from a site, it lands here with the day we last saw it and the day we saw it gone.",
    removedOn: "Removed",
    confirmedOn: "Match reviewed",
    lastChecked: "Last checked",
    neverChecked: "Not checked yet",
    standing: {
      handled: "No copy online",
      removed_case_open: "Removed here · record still open",
      reopened: "Back online",
    },
    reappeared: "Back online since {{date}}.",
  },
  invoices: {
    heading: "Invoices",
    lead: "Your invoices, newest first. Please pay by bank transfer and quote the invoice number; the bank details and a code for your banking app are on each PDF.",
    empty: "There are no invoices yet.",
    emptyNext: "What happens next: once a subscription period has started, the invoice for it appears here as a PDF, with the bank details and the number to quote.",
    notOnline: "Invoices are not shown here yet. We send each invoice to you as a PDF by email.",
    invoiceTitle: "Invoice {{number}}",
    cancellationTitle: "Cancellation {{number}}",
    issuedOn: "Issued",
    dueOn: "Due",
    period: "Service period",
    paidOn: "Payment received",
    cancelledBy: "Cancelled by {{number}}.",
    download: "Download PDF",
    status: {
      open: "Open",
      overdue: "Overdue",
      paid: "Paid",
      cancelled: "Cancelled",
      issued: "No payment due",
    },
    vat: {
      domestic: "Includes German VAT.",
      reverse_charge: "No German VAT: reverse charge applies.",
      small_business: "No VAT charged (small-business scheme, § 19 UStG).",
    },
  },
  shell: {
    brand: "Marceline",
    tagline: "Find out where your work has been copied to.",
    documentTitle: "Marceline",
    signedInAs: "Signed in as {{username}}",
    downloadData: "Download my data",
    signOut: "Sign out",
    signingOut: "Signing out…",
    skipToContent: "Skip to content",
    noImagery: "We never show copies of your material — no thumbnails, no previews. What we show is the site, the address and the dates.",
    // The banner that asks for the address to be confirmed (ADR 0054 §3). It **blocks nothing** and
    // says so; the words are in `core` because the shell renders them on every screen.
    verifyEmail: "We sent a mail to your address; opening the link in it tells us it reaches you.",
    verifyEmailAction: "Send it again",
    // The address every "write to us" in this app means: there is no self-service password reset,
    // no signup and no way to ask for an export or a deletion from a screen, so the one way to
    // reach a human has to be on every page. Same string in both languages on purpose.
    contact: "Questions, or something not working? Write to us:",
    contactEmail: "contact@marceline.studio",
    // Shown on `/app/export` (issue #131): the export link redirects here instead of saving a
    // refusal as a file. Short on purpose — `nav.overview` is the way back.
    export: {
      heading: "Your data",
      rateLimited: "You can download your data once an hour. Try again in an hour.",
      notAvailable: "The export is not available for this account.",
      error: "Something went wrong. Write to us if this keeps happening.",
    },
  },
  signIn: {
    heading: "Sign in",
    lead: "Sign in to see where copies matching your work have turned up and what has happened since.",
    username: "Email address",
    password: "Password",
    submit: "Sign in",
    submitting: "Signing in…",
    accountsNote: "We set up accounts ourselves: write to us if you need access.",
    // The one link out of the sign-in form (ADR 0054). In `core` because this form is in `core`, and
    // it is two words — the screens it leads to bring their own slice.
    forgot: "Forgotten your password?",
    errors: {
      invalid: "That email address and password do not match an account.",
      noOrganisation: "Your password is correct, but this account is not connected to a customer organisation. If you work for us, use the internal review interface instead.",
      formExpired: "The sign-in form expired. Please try again.",
      rateLimited: "Too many sign-in attempts. Please try again later.",
      rateLimitedMinutes_one: "Too many sign-in attempts. Please try again in {{count}} minute.",
      rateLimitedMinutes_other: "Too many sign-in attempts. Please try again in {{count}} minutes.",
      rateLimitedSeconds_one: "Too many sign-in attempts. Please try again in {{count}} second.",
      rateLimitedSeconds_other: "Too many sign-in attempts. Please try again in {{count}} seconds.",
      unexpected: "Sign-in failed ({{status}}). Please try again.",
    },
  },
  home: {
    heading: "Your account",
    lead: "You are signed in. Everything you see here belongs to this organisation and to no other.",
    organisation: "Organisation",
    segment: "Account type",
    role: "Your role",
    segments: {
      professional: "Professional",
      enterprise: "Enterprise",
    },
    roles: {
      owner: "Owner",
      member: "Member",
    },
    noOrganisation: "This account is not connected to an organisation, so it owns nothing yet. Please write to us.",
  },
  caseFile: {
    where: {
      open: "Open the page in a new tab",
      openWarning: "Sites like this often show aggressive advertising. We never load this page for you — only you can open it.",
    },
  },
  addOriginal: {
    submissions: {
      heading: "Originals you sent",
      status: {
        processing: "Being added",
        done: "Added",
        refused: "Not added",
        failed: "Not added yet",
      },
      processing: "Received. It appears in your overview once it has been added.",
      done: "Added to your account.",
      failed: "It could not be added automatically. We are looking into it and will get in touch with you.",
    },
  },
  rights: {
    works: {
      missing: "Still needed before a notice draft could be prepared:",
      blockers: {
        work_not_owned: "The work is not registered to your organisation.",
        claimant_type_missing: "Choose a claimant type.",
        identity_not_verified: "Our staff have not checked your identity yet.",
        rights_record_missing: "No self-declaration for this work yet.",
        documents_not_complete: "Our staff have not checked your documents for completeness yet.",
        authority_not_declared: "Your own declaration about the right or authority your documents give you is still missing.",
        declaration_incomplete: "Complete your self-declaration: say in which capacity you act and which right you hold.",
        rights_disputed: "The rights to this work are disputed.",
        rights_revoked: "The rights or authority were revoked.",
        rights_expired: "The rights or authority have expired.",
        competing_claim: "Another party claims rights to this work.",
        underlying_owner_missing: "Name the rights holder you act for.",
        mandate_missing: "No agency mandate is on record for this work.",
        mandate_not_yet_valid: "The agency mandate is not valid yet.",
        mandate_expired: "The agency mandate has expired.",
        mandate_revoked: "The agency mandate was revoked.",
        mandate_notice_clause_not_recorded: "Our record of this mandate has no entry for notices. We do not assess what your documents mean legally — please write to us.",
        schedule_expired: "The catalogue schedule covering this work has expired.",
        partners_not_affirmed: "List your licensed partners, or declare that there are none.",
      },
    },
  },
};

/** The shape every language must match for this slice. */
export type CoreWords = typeof enCore;
