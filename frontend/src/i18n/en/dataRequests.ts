/**
 * `/app/data` — asking for a copy of your data, or for its deletion (ADR 0056, issue #133).
 *
 * Its own slice, like `onboarding` and `verification`: only the Next customer app has the screen,
 * and the SPA is the rollback rather than the future. Not in `src/i18n/slices.ts` for that reason.
 *
 * Two rules these strings exist under, both from the ADR:
 *
 * - **a request is a request, never an answer.** Nothing here says data was exported or deleted.
 *   Both are done by a person, and the screen says what the person will do rather than promising an
 *   outcome or a moment. The statutory month is named because it is the law's, not ours.
 * - **the deletion is irreversible and reads that way.** What is kept (invoices, a legal hold), what
 *   the window is, and the typed word: the sentence a creator reads before pressing it is the point
 *   of that word, not an obstacle in front of it.
 */
export const enDataRequests = {
  dataRequests: {
    /**
     * The shell's link to this screen. In **this slice** and not in `core.ts`, although the Next
     * app's shell renders it on every page: only that app has the screen, and it renders on the
     * server from the whole composed resource, so a word here costs a browser nothing. `core.ts` is
     * what every SPA `/app` visit downloads and its budget (4.5 KB) is nearly spent — the rule in
     * `frontend/CLAUDE.md` is about the SPA's per-route loading, and the SPA's own shell never
     * names this key.
     */
    navLink: "My data",
    heading: "My data",
    lead: "Ask for a copy of everything your account holds, or ask us to delete it and end your pilot. A person answers both by hand.",
    // Django answers 404 for the whole feature when it is not switched on for this machine: a
    // sentence, never an error — the footer's address still works, which is what it says.
    notOnline:
      "Asking from here is not switched on yet. Write to us instead and we will do the same thing by hand.",
    deadline: "We answer within one month (Art. 12(3) GDPR).",
    contact: "You can also simply write to {{email}} — no form, no proof beyond your account.",
    export: {
      heading: "Send me a copy of my data",
      body: "Everything your account holds: your originals and their stored fingerprints, the matching copies we recorded, your rights records, your notice drafts and the invoices. One machine-readable file (Art. 15, Art. 20 GDPR).",
      submit: "Ask for a copy",
      submitting: "Sending…",
      open: "Asked for on {{date}}.",
      done: "Answered on {{date}}.",
      empty: "You have not asked for a copy yet.",
    },
    deletion: {
      heading: "Delete my data and end my pilot",
      body: "A person sets the end date, and everything your account holds is deleted 30 days after it — the window is there so you can still take your copy with you.",
      kept: "Two things stay: invoices, which tax law makes us keep, and anything under a legal hold. Our own index of pages is not your data and is not part of this.",
      phrase: "Type {{phrase}} to send the request. This cannot be undone once we have acted on it.",
      phraseLabel: "The word above",
      submit: "Ask for deletion",
      submitting: "Sending…",
      open: "Asked for on {{date}}. You can take it back until we act on it.",
      done: "Answered on {{date}}.",
      empty: "You have not asked for a deletion.",
    },
    status: {
      heading: "Where it stands",
      requested: "Received",
      in_progress: "Being worked on",
      done: "Answered",
      withdrawn: "Taken back",
    },
    due: "Answer due by {{date}}.",
    withdraw: "Take this request back",
    withdrawing: "Taking it back…",
    history: {
      heading: "Earlier requests",
      requestedOn: "Asked for on {{date}}",
      settledOn: "Settled on {{date}}",
    },
    pending: "Received. It will show here shortly.",
    errors: {
      already_open: "That request is already open — we have it.",
      not_open: "There is no open request of that kind any more.",
      confirmation_mismatch: "The word did not match. Type it exactly as it is shown.",
      unknown_kind: "That is not something you can ask for here.",
      data_request_write_unavailable:
        "Requests cannot be taken right now. Write to us and we will record it.",
      processing_paused: "Requests cannot be taken right now. Please try again later.",
      too_many_requests: "That was a lot of requests at once. Please try again later.",
      generic: "That did not work. Please try again, or write to us.",
    },
  },
};

/** The shape the German slice must match exactly (`de/dataRequests.ts`). */
export type DataRequestWords = typeof enDataRequests;
