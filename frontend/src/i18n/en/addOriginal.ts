/**
 * The "add an original" screen's words (`/app/originals/new`), loaded by that route alone —
 * including the cloud-fingerprinting words of ADR 0044, which no longer need a module of their own
 * (`upload.ts`, removed 2026-09-18): a screen's words now cost only the route that shows them.
 *
 * `addOriginal.submissions` is **not** here: the overview lists uploads being added, so those keys
 * live in `core.ts` and `en.ts` merges the section together.
 */

export const enAddOriginal = {
  addOriginal: {
    heading: "Add an original",
    lead: "Your video stays on this computer. Your browser reads it and computes fingerprints — short codes that describe its images. Only those are sent to us, with the title, the names below, the length, the file size and a checksum.",
    back: "Back to your originals",
    unavailable: "Adding originals is not available here yet. Write to us and we add them for you.",
    /**
     * Said before the form, always, on every device (issue #117). The audience arrives on a
     * phone, and the honest thing to tell them is where this step is hard: the browser reads the
     * whole file, which is slow on a phone and, for a large file, beyond some mobile browsers
     * entirely. It promises nothing about sending the file instead — that option is off
     * (ADR 0044) and may be off for a given account forever.
     *
     * Since issue #116 it says which of the two kinds is hard, because only one is: a photo set
     * is read one picture at a time and a photograph is small (ADR 0053), so a creator on a phone
     * who was going to add a set should not be told to find a computer first.
     */
    onAPhone: "A video works best on a computer. Your browser has to read the whole file to compute the fingerprints: on a phone that takes much longer, and some mobile browsers cannot read a large file at all. A photo set is fine on a phone — each picture is small, and they are read one at a time.",
    file: {
      label: "Video file",
      hint: "MP4 or MOV with H.264 works best, in Chrome or Edge. Up to {{hours}} hours.",
      chosen: "{{name}} · {{size}}",
    },
    title: {
      label: "Title",
      hint: "How the original is named in your account.",
    },
    rightsHolder: {
      label: "Rights holder",
      hint: "The performer or studio who holds the rights in this video, as credited.",
    },
    others: {
      label: "Other performers (optional)",
      hint: "One name per line.",
    },
    rights: {
      legend: "Your declaration",
      // Version 2026-09-18, word for word from backend/marceline/app/rights_confirmation.json —
      // kind-neutral, because an original is a video or a photo set (ADR 0053 §4).
      statement: "I own, or am authorised to enforce, the rights in this original.",
    },
    // What is being added (issue #116, ADR 0053). The choice comes first, because it changes which
    // files the picker takes and what every word below is about.
    kind: {
      legend: "What are you adding?",
      video: "A video",
      photos: "A photo set",
    },
    photos: {
      lead: "Your photos stay on this computer. Your browser reads them one by one and computes fingerprints — short codes that describe each image. Only those are sent to us, with the title, the names below, the number of photos, their total size and a checksum. No photo, and no file name.",
      file: {
        label: "Photo files",
        hint: "JPEG, PNG, WebP or AVIF. Choose all the photos of one set at once — they are added as one original. Up to {{count}} photos.",
        chosen_one: "{{count}} photo · {{size}}",
        chosen_other: "{{count}} photos · {{size}}",
      },
      rightsHolder: {
        hint: "The performer or studio who holds the rights in these photos, as credited.",
      },
      progress: {
        keepOpen: "Keep this tab open until it is done. Your photos are not uploaded.",
        images: "Computing fingerprints",
        imagesCount: "{{done}} of {{total}} photos",
      },
      skipped_one: "{{count}} file could not be read on this computer and is not part of the set. HEIC photos have to be exported as JPEG first.",
      skipped_other: "{{count}} files could not be read on this computer and are not part of the set. HEIC photos have to be exported as JPEG first.",
      problems: {
        missingFiles: "Choose the photos of the set.",
      },
      errors: {
        noUsableImages: "None of these photos could be read on this computer.",
        codecUnsupported: "This browser could not read these photos. JPEG, PNG, WebP and AVIF work; export an HEIC set as JPEG first.",
      },
    },
    // Cloud fingerprinting (issue #59, ADR 0044): the second method, offered only where the
    // account may use it. The words promise no duration — `wording.test.ts` bans "within N
    // hours/days", "guarantee", "expect" and "likely", and this is the screen where one would be
    // tempting.
    method: {
      legend: "Fingerprints",
      local: "On this computer (recommended)",
      localHint: "Only the fingerprints are sent. The file stays here.",
      upload: "Send the file to us",
      uploadHint:
        "For files this browser cannot read. We compute the fingerprints, delete the file and keep only them. Nobody looks at it.",
    },
    upload: {
      start: "Send the file and add",
      // The declaration a file may only be sent with (ADR 0044 amendment 2026-09-19).
      // `solePerformer` and `article9Consent` are the server's own words, checked against
      // `backend/marceline/app/upload_declaration.json` in `addOriginal.test.ts`: what is
      // stored with the upload must be what was on the screen. Draft wording.
      declaration: {
        legend:
          "Declaration for this file",
        solePerformer:
          "I declare that I am of age and the only person visible or audible in this file. No other person appears in it. I made the recording myself or had it made, and I am entitled to dispose of it.",
        article9Consent:
          "I agree that Marceline may process this file in order to compute fingerprints from it, and I explicitly consent to the processing, for that purpose, of any special categories of personal data the recording may contain about me (Art. 9(2)(a) GDPR). The file is deleted once the fingerprints are computed; only the fingerprints and the details I entered myself are kept. I may withdraw this consent at any time, without affecting the processing carried out until then.",
        others:
          "Is anyone else in the file? Then compute the fingerprints on this computer instead — the file stays with you.",
      },
      progress: {
        heading: "Sending “{{title}}”",
        keepOpen: "Keep this tab open until the file has been sent.",
        sending: "Sending the file",
      },
      // What happens after the last chunk (ADR 0044 amendment 2026-09-18): the screen asks the
      // server how far the fingerprinting has come. No duration anywhere.
      watch: {
        heading: "Fingerprints for “{{title}}”",
        leave: "You can close this page. The work runs on our server, and your overview shows how far it has come.",
        hashing: "Computing the fingerprints",
        frames: "{{done}} of {{total}} moments",
        framesWaiting: "Reading the file",
        added: "“{{title}}” has been added.",
        failed: "“{{title}}” was not added.",
        fallback: "You can compute the fingerprints on this computer instead.",
        deleted: "The file has been deleted. We keep only the fingerprints.",
        unreachable: "We cannot read the status just now. The work carries on; your overview shows it.",
      },
      /*
       * `open`, `state` and `reasons` word the list of files still being added, which the **Next
       * app** shows on its overview as well as here (`components/OpenUploads.tsx`). They stay in
       * this slice rather than in `core.ts` because the Vite SPA has no upload screen at all and
       * no list either: it would pay for words nothing of it can show, which is exactly what
       * `CORE_WORDS_BUDGET_KB` is there to prevent, and the Next app holds the whole resource at
       * request time (`customer/lib/i18n.ts`) so its overview has them regardless. The day the SPA
       * grows that list, move these three groups to `core.ts` and merge `addOriginal.upload` in
       * `en.ts`/`de.ts` the way `caseFile.where` is already merged.
       */
      open: {
        heading: "Files being added",
      },
      state: {
        receiving: "Sending this file was not finished.",
        queued: "Sent. Waiting to be fingerprinted.",
        hashing: "The fingerprints are being computed.",
        done: "Added.",
        refused: "Not added.",
        failed: "Not added.",
        expired: "Not added.",
        cancelled: "Cancelled.",
      },
      reasons: {
        unsupported_format:
          "We cannot read this file type. You can compute the fingerprints on this computer instead.",
        too_long: "This file is longer than we can fingerprint.",
        too_large: "This file is larger than we can fingerprint.",
        decode_failed:
          "We could not read this file to the end. Another export of the same video usually works.",
        timed_out: "Fingerprinting this file took too long and was stopped.",
        quota: "You have sent a lot today. Please add the rest another day.",
        incomplete: "The file did not arrive complete. Please send it again.",
        needs_review: "A person has to look at this file before it can be added.",
        cancelled: "You cancelled this one.",
        internal: "Something went wrong on our side. Please try again.",
      },
    },
    problems: {
      heading: "Before we can start:",
      missingFile: "Choose a video file.",
      tooManyImages: "That is more photos than we can add at once.",
      missingTitle: "Enter a title.",
      titleTooLong: "The title is too long.",
      missingRightsHolder: "Name the rights holder.",
      tooManyNames: "Name at most 20 people.",
      nameTooLong: "One of the names is too long.",
      notConfirmed: "Tick the declaration.",
      notDeclared: "Tick both statements for this file.",
    },
    start: "Compute fingerprints and add",
    cancel: "Cancel",
    progress: {
      heading: "Working on “{{title}}”",
      keepOpen: "Keep this tab open until it is done. Your video is not uploaded.",
      digest: "Reading the file",
      frames: "Computing fingerprints",
      framesCount: "{{done}} of {{total}} positions",
      framesWaiting: "Opening the video…",
      uploading: "Sending the fingerprints…",
      etaSoon: "Less than a minute left.",
      eta_one: "About {{count}} minute left on this device.",
      eta_other: "About {{count}} minutes left on this device.",
    },
    done: {
      created: "“{{title}}” has been added. We now compare it with the sources we watch; matching copies reviewed by a person appear in your account.",
      existing: "“{{title}}” is already in your account, so nothing was added twice.",
      received: "“{{title}}” was received and is being added. It appears in your overview once it has been added; the overview also says if it could not be added.",
      open: "Open the original",
      overview: "Back to the overview",
      another: "Add another original",
    },
    errors: {
      heading: "The original was not added",
      tryAgain: "Try again",
      codecUnsupported: "This browser cannot read the video. Try Chrome or Edge with an MP4 or MOV file (H.264), or write to us.",
      local: {
        cancelled: "Cancelled. Nothing was sent.",
        too_long: "This video is too long to add here. Write to us and we add it for you.",
        too_many_images: "This set has more photos than we can add at once. Write to us and we add it for you.",
        no_duration: "The file does not say how long it is, so it cannot be read.",
        no_usable_frames: "The video has too few distinct images to compute fingerprints from.",
        other: "Something went wrong while reading the files on this computer. Please try again.",
      },
      server: {
        quota_exceeded: "You have sent a lot today. Please add the rest another day.",
        spool_full: "We cannot take another file just now. Please try again later.",
        claim_needs_review: "We need to check this original before it can be added, because it is already registered. We will get in touch with you.",
        rights_confirmation_required: "The declaration was not accepted. Please reload the page and tick it again.",
        too_long: "This video is too long to add here. Write to us and we add it for you.",
        too_many_images: "This set has more photos than we can add at once. Write to us and we add it for you.",
        adding_originals_unavailable: "Adding originals is not available here yet. Write to us and we add them for you.",
        processing_paused: "Adding originals is paused at the moment. Nothing was saved. Please try again later.",
        no_organisation: "This account is not connected to an organisation, so it cannot add originals.",
        body_too_large: "This is too large to add here. Write to us and we add it for you.",
        unreachable: "We could not reach the server. Your files were read, but nothing was saved. Please try again.",
        too_many_requests: "You have added many originals in a short time. Please try again later.",
        too_many_requests_minutes_one: "You have added many originals in a short time. Please try again in {{count}} minute.",
        too_many_requests_minutes_other: "You have added many originals in a short time. Please try again in {{count}} minutes.",
        signed_out: "You have been signed out. Please sign in again and repeat this.",
        invalid: "The fingerprints were not accepted. Please try again; if it happens again, write to us.",
        unexpected: "Sending the fingerprints failed. Please try again.",
      },
    },
  },
};

/** The shape every language must match for this slice. */
export type AddOriginalWords = typeof enAddOriginal;
