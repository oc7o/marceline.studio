/**
 * What the demo adds around the real app: one slim line saying what this is, and the short
 * "this is a demo" messages the mock API answers a write with — both in the app's own tokens.
 */
import { type ReactNode, useEffect, useState } from "react";

import { onDemoMessage } from "./mock/notify";
import { usePageLanguage } from "./runtime/router";

const SOURCE_URL = "https://github.com/oc7o/marceline.studio";
const IMPRINT_URL = "https://sloow.de/impressum/";

const WORDS = {
  en: {
    label: "About this demo",
    text: "Archived demo · sample data · nothing you enter is sent anywhere",
    source: "Source",
    imprint: "Imprint",
  },
  de: {
    label: "Über diese Demo",
    text: "Archivierte Demo · Beispieldaten · nichts, was du eingibst, wird irgendwohin gesendet",
    source: "Quellcode",
    imprint: "Impressum",
  },
} as const;

// `tap` is the app's own class for a standalone link: 44 px to hit under a finger, nothing on a mouse.
const LINK = "tap underline decoration-hairline-strong underline-offset-2 hover:text-ink";

function DemoBanner() {
  const words = WORDS[usePageLanguage()];
  return (
    <aside
      aria-label={words.label}
      className="flex items-center gap-x-3 border-b border-hairline bg-surface-200 px-4 text-caption text-ink-muted sm:justify-center sm:gap-x-4 sm:py-1.5 sm:text-body-s"
    >
      <span className="min-w-0 py-1 sm:py-0">{words.text}</span>
      <span className="flex shrink-0 gap-x-3 sm:gap-x-4">
        <a href={SOURCE_URL} target="_blank" rel="noopener noreferrer" className={LINK}>
          {words.source}
        </a>
        <a href={IMPRINT_URL} target="_blank" rel="noopener noreferrer" className={LINK}>
          {words.imprint}
        </a>
      </span>
    </aside>
  );
}

const TOAST_MS = 6000;

function DemoToast() {
  const [message, setMessage] = useState<{ text: string; id: number } | null>(null);

  useEffect(() => onDemoMessage((text) => setMessage({ text, id: Date.now() })), []);
  useEffect(() => {
    if (!message) return;
    const timer = setTimeout(() => setMessage(null), TOAST_MS);
    return () => clearTimeout(timer);
  }, [message]);

  return (
    <div
      role="status"
      aria-live="polite"
      // Above the app's bottom bar on a phone (2.75rem plus the device's own inset), at the edge from `sm`.
      className="pointer-events-none fixed inset-x-0 bottom-[calc(3.5rem_+_env(safe-area-inset-bottom))] z-50 flex justify-center px-4 sm:bottom-4"
    >
      {message ? (
        <p className="pointer-events-auto max-w-md rounded-md border border-hairline-strong bg-ink px-4 py-2 text-body-s text-ink-inverse">
          {message.text}
        </p>
      ) : null}
    </div>
  );
}

export function DemoChrome({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-full flex-col">
      <DemoBanner />
      {/* A grid cell stretches its child, so the app's own `min-h-full` shell still fills the page.
          `minmax(0, 1fr)`: an automatic column is as wide as its content at least, and the shell
          must be as wide as the page, as it is as a block child of `body` in the real app. */}
      <div className="grid flex-1 grid-cols-[minmax(0,1fr)]">{children}</div>
      <DemoToast />
    </div>
  );
}
