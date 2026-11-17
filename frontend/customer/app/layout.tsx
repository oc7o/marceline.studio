/**
 * The document every customer page is rendered into (ADR 0036).
 *
 * `<html lang>` is right in the first byte, because the language is a property of the request
 * (`lib/i18n.ts`), not something JavaScript decides after the page has painted.
 *
 * Every page under `/app` is one customer's own data, so nothing here is prerendered or cached.
 */
import type { Metadata } from "next";
import type { ReactNode } from "react";

import { requestI18n } from "../lib/i18n";
import "./globals.css";

export const dynamic = "force-dynamic";

export async function generateMetadata(): Promise<Metadata> {
  const { t } = await requestI18n();
  return {
    title: t("shell.documentTitle"),
    description: t("shell.tagline"),
    // Nothing in here is for a search engine, and no page may leak a referrer.
    robots: { index: false, follow: false },
    referrer: "no-referrer",
  };
}

export default async function RootLayout({ children }: { children: ReactNode }) {
  const { language } = await requestI18n();
  return (
    <html lang={language} className="h-full">
      {/*
        One font is preloaded, and only one: the body face. Every word on every screen that is not
        a heading is set in Instrument Sans, so it is on the critical path for the first paint of
        real content, and without this hint the browser only discovers it once the stylesheet has
        parsed — a round trip late.

        Instrument Serif is deliberately *not* preloaded, and that is a closer call than it looks:
        it is the heading face (the wordmark, every page title, every section heading), so it is
        above the fold on every screen. Three things settle it. There is one stylesheet, so the
        browser discovers both faces at the same moment and a preload buys the serif about one
        round trip, not a fetch it would otherwise miss. That round trip costs 21 KB of the same
        connection's top priority, taken from the body text, which is the larger surface. And its
        fallbacks — Iowan Old Style, Georgia — are real serifs on the systems that have them, so
        the swap is a change of serif, not serif to sans. If a cold load ever shows the headings
        visibly reflowing, a second preload here is the fix, and this is the trade to revisit.

        JetBrains Mono is not preloaded either, for a plainer reason: it is fetched only on a
        screen that actually renders a machine-readable string, and a preload would turn that into
        a cost on every screen.

        `crossOrigin` is required even same-origin: fonts are fetched in CORS mode, and a preload
        without it is simply a second, wasted download. React hoists this into `<head>`.
      */}
      <link
        rel="preload"
        // `/app` because `basePath` does not reach a static file; the stylesheet's font references
        // carry it for the same reason, and `app/globals.css` says why.
        href="/app/fonts/instrument-sans-latin.c10e9d3f.woff2"
        as="font"
        type="font/woff2"
        crossOrigin="anonymous"
      />
      {/* The ground and the ink are the theme's, set on `body` in `globals.css`. */}
      <body className="h-full antialiased">{children}</body>
    </html>
  );
}
