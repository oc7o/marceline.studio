"use client";
/**
 * Sign out: one POST to this server's own route handler, which forwards it to Django and passes
 * Django's cookie flush back (ADR 0036). Then the whole app is asked for again, so a signed-out
 * customer is never left looking at their own organisation.
 */
import { useRouter } from "next/navigation";
import { useState } from "react";

import { csrfToken } from "../lib/csrf";

export function SignOutButton({ label, busyLabel }: { label: string; busyLabel: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  const signOut = async () => {
    setBusy(true);
    try {
      await fetch("/app/api/logout", {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken() ?? "" },
        credentials: "same-origin",
      });
    } finally {
      // Whatever happened, ask the server again: it is the only thing that knows whether there is
      // still a session, and the gate in the signed-in layout acts on its answer.
      router.replace("/sign-in");
      router.refresh();
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      onClick={() => void signOut()}
      disabled={busy}
      className="inline-flex items-center gap-2 rounded-sm border border-border-control bg-surface-100 px-3 py-1.5 text-button text-ink transition-colors duration-120 ease-standard hover:bg-surface-300 disabled:cursor-not-allowed disabled:opacity-45"
    >
      {busy ? busyLabel : label}
    </button>
  );
}
