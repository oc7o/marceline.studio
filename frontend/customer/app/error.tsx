"use client";

/**
 * The error boundary for everything under the root layout (issue #132).
 *
 * Before this, an uncaught throw while a screen rendered fell to Next's own built-in page: no
 * navigation, no contact address, not our language, nothing but the browser's back button. Every
 * page catches its own read (`lib/pageReads.ts`) and the session gate renders `common.unreachable`
 * for anything that is not a 401 — this is the boundary for what neither of those catches: a
 * screen that throws on a shape it did not expect.
 *
 * Next requires an error boundary to be a Client Component (it receives `reset`), and the root
 * layout is still intact when this renders, so the stylesheet and the design tokens are already
 * loaded — `globals.css` is not repeated here the way it is in `global-error.tsx`.
 *
 * **No stack trace, ever.** `error.message` and `error.stack` are never rendered; Django's own
 * sentences are the only source of an error message anywhere else in this app, and a raw
 * exception is not one. The words are written out in both languages rather than read from
 * `src/i18n`: this boundary has no server request to ask for a language, and the two resource
 * files must not ship to the browser as a client chunk (issue #99) — the whole point of the split
 * this file sits outside of.
 */
export default function ErrorBoundary({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-md flex-col items-center justify-center gap-4 p-6 text-center">
      <div className="flex flex-col gap-1">
        <p lang="de" className="text-body text-ink">
          Etwas ist schiefgelaufen. Versuch es noch einmal.
        </p>
        <p lang="en" className="text-body text-ink-muted">
          Something went wrong. Try again.
        </p>
      </div>
      <div className="flex flex-wrap items-center justify-center gap-3">
        <button
          type="button"
          onClick={reset}
          className="tap rounded-sm border border-border-control bg-surface-100 px-3 py-1.5 text-button text-ink hover:bg-surface-300"
        >
          <span lang="de">Noch einmal versuchen</span> <span lang="en">· Try again</span>
        </button>
        <a
          href="/app"
          className="tap rounded-sm border border-brand bg-brand px-3 py-1.5 text-button text-on-brand hover:bg-brand-hover"
        >
          <span lang="de">Zur Übersicht</span> <span lang="en">· Overview</span>
        </a>
      </div>
      <p className="text-caption text-ink-subtle">
        <span lang="de">Schreib uns, wenn das öfter passiert:</span>{" "}
        <span lang="en">Write to us if this keeps happening:</span>{" "}
        <a href="mailto:contact@marceline.studio" className="break-all underline">
          contact@marceline.studio
        </a>
      </p>
    </main>
  );
}
