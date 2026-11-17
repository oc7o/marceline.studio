"use client";

/**
 * The last-resort boundary (issue #132): only reached when the root layout itself
 * (`app/layout.tsx`) throws while rendering, which `app/error.tsx` cannot catch — Next replaces
 * the whole document with this one, so it defines its own `<html>` and `<body>` rather than
 * relying on the layout that just failed.
 *
 * **No Tailwind class, no `globals.css`, no font**: the layout that links the stylesheet is
 * exactly what did not render, so nothing here may depend on it having loaded. Inline styles only,
 * and the smallest amount of markup that still says something and offers a way out. Same "no
 * stack trace, two languages written out" rule as `app/error.tsx` — see that file for why.
 */
import type { CSSProperties } from "react";

const PAGE: CSSProperties = {
  display: "flex",
  minHeight: "100dvh",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  gap: "1rem",
  padding: "1.5rem",
  textAlign: "center",
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
  color: "#1a1a1a",
  background: "#ffffff",
};
const LINK: CSSProperties = {
  color: "#1a1a1a",
  textDecoration: "underline",
};

export default function GlobalError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <html lang="en">
      <body>
        <main style={PAGE}>
          <p lang="de">Etwas ist schiefgelaufen. Versuch es noch einmal.</p>
          <p lang="en">Something went wrong. Try again.</p>
          <p>
            <button type="button" onClick={reset} style={LINK}>
              <span lang="de">Noch einmal versuchen</span> <span lang="en">· Try again</span>
            </button>
          </p>
          <p>
            <a href="/app" style={LINK}>
              <span lang="de">Zur Übersicht</span> <span lang="en">· Overview</span>
            </a>
          </p>
          <p>
            <span lang="de">Schreib uns, wenn das öfter passiert:</span>{" "}
            <span lang="en">Write to us if this keeps happening:</span>{" "}
            <a href="mailto:contact@marceline.studio" style={LINK}>
              contact@marceline.studio
            </a>
          </p>
        </main>
      </body>
    </html>
  );
}
