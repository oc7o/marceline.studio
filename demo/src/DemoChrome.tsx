/**
 * What the demo adds around the real app: one slim line saying what this is, and the short
 * "this is a demo" messages the mock API answers a write with.
 */
import { type ReactNode, useEffect, useState } from "react";

import { onDemoMessage } from "./mock/notify";

const SOURCE_URL = "https://github.com/oc7o/marceline.studio";
const IMPRINT_URL = "https://sloow.de/impressum/";

const LINK = "underline decoration-hairline-strong underline-offset-2 hover:text-ink";

function DemoBanner() {
  return (
    <aside
      aria-label="About this demo"
      className="flex flex-wrap items-center justify-center gap-x-4 gap-y-0.5 border-b border-hairline bg-surface-200 px-4 py-1.5 text-center text-body-s text-ink-muted"
    >
      <span>Archived demo · sample data · nothing you enter is sent anywhere</span>
      <span className="flex gap-x-4">
        <a href={SOURCE_URL} target="_blank" rel="noopener noreferrer" className={LINK}>
          Source
        </a>
        <a href={IMPRINT_URL} target="_blank" rel="noopener noreferrer" className={LINK}>
          Imprint
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
    <div role="status" aria-live="polite" className="pointer-events-none fixed inset-x-0 bottom-4 z-50 flex justify-center px-4">
      {message ? (
        <p className="pointer-events-auto max-w-md rounded-md border border-hairline-strong bg-ink px-4 py-2 text-body-s text-ink-inverse shadow-md">
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
      {/* A grid cell stretches its child, so the app's own `min-h-full` shell still fills the page. */}
      <div className="grid flex-1">{children}</div>
      <DemoToast />
    </div>
  );
}
