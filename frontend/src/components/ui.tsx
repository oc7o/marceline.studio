/**
 * The shared primitives of the staff moderation SPA, on the design system's tokens
 * (`src/index.css`).
 *
 * Three of the system's rules decide almost everything here, and they are worth stating once:
 *
 * - **Radius carries meaning.** Anything holding evidence stays square; chrome softens. These are
 *   chrome, so a control is `rounded-sm`, a card `rounded-md`, a dialog `rounded-lg`, and a status
 *   pill is the one `rounded-full` thing in the system. The queue tables, hash fields and thumbnail
 *   frames that hold the evidence keep `rounded-none` where they are built.
 * - **Automation is violet.** `accent` marks what the machine decided — a PDQ match, a cluster the
 *   builder proposed, a row `decided_by=auto`. A human decision is neutral or red. `Badge` carries
 *   the distinction, which is why its tones are named for meaning and not for hue.
 * - **Colour never works alone.** Every badge renders its state as a word; the ground repeats it.
 *
 * The focus ring is not set here: `src/index.css` states it once for every focusable element,
 * including rows and cards, so no component can drop it.
 */
import { type ReactNode, useEffect, useRef } from "react";

import { SHORTCUTS } from "../lib/keyboard";
import { BADGE_TONES, type BadgeTone } from "../lib/tones";

/**
 * Cluster-member and pair-label states. `proposed` is the machine's word and takes the violet;
 * `confirmed` and `rejected` are a person's and take the teal and the red.
 */
const STATUS_STYLES: Record<string, string> = {
  proposed: "bg-accent-quiet text-accent",
  confirmed: "bg-positive-quiet text-positive-ink",
  rejected: "bg-critical-quiet text-critical-ink",
  conflict: "bg-caution-quiet text-caution-ink",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-block rounded-full px-2 py-0.5 text-overline uppercase ${
        STATUS_STYLES[status] ?? "bg-surface-300 text-ink"
      }`}
    >
      {status}
    </span>
  );
}

/** Tones named for what they mean, not for a colour — see `lib/tones.ts`. */
export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: BadgeTone }) {
  return (
    <span className={`inline-block rounded-sm px-1.5 py-0.5 text-body-s ${BADGE_TONES[tone]}`}>
      {children}
    </span>
  );
}

/**
 * `primary` and `reject` share the red on purpose: rejecting *is* the red act, and the two never
 * stand next to each other (a view has one primary action, in a dialog or a header). The
 * distinction the reviewer's hand needs is confirm (teal) against reject (red), and that one is
 * the whole width of the palette apart.
 */
type ButtonTone = "neutral" | "confirm" | "reject" | "primary";

export function Button({
  children,
  tone = "neutral",
  shortcut,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { tone?: ButtonTone; shortcut?: string }) {
  const tones: Record<ButtonTone, string> = {
    neutral: "border-hairline-strong bg-surface-100 text-ink hover:bg-surface-300",
    confirm: "border-positive-ink bg-positive-ink text-ink-inverse hover:opacity-90",
    reject: "border-critical bg-critical text-on-brand hover:bg-brand-hover",
    primary: "border-brand bg-brand text-on-brand hover:bg-brand-hover",
  };
  return (
    <button
      type="button"
      {...props}
      className={`inline-flex items-center gap-2 rounded-sm border px-3 py-1.5 text-button transition-[background-color,opacity] duration-120 ease-standard disabled:cursor-not-allowed disabled:opacity-45 ${tones[tone]} ${props.className ?? ""}`}
    >
      {children}
      {shortcut ? (
        <kbd className="rounded-xs border border-current/30 px-1 font-mono text-mono-s opacity-80">
          {shortcut}
        </kbd>
      ) : null}
    </button>
  );
}

export function Dialog({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const focusable = ref.current?.querySelector<HTMLElement>("textarea, input, button");
    focusable?.focus();
  }, []);
  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-scrim p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      {/* A dialog is the one layer that really floats, so it is the one that gets `shadow-lg`. */}
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        className="w-full max-w-lg rounded-lg border border-hairline bg-surface-100 p-5 shadow-lg"
      >
        <h2 id="dialog-title" className="mb-3 font-display text-display-s">
          {title}
        </h2>
        {children}
      </div>
    </div>
  );
}

export function ShortcutsDialog({ onClose, extra }: { onClose: () => void; extra?: string }) {
  return (
    <Dialog title="Keyboard shortcuts" onClose={onClose}>
      <table className="w-full text-body-s">
        <tbody>
          {SHORTCUTS.map((shortcut) => (
            <tr key={shortcut.keys} className="border-b border-hairline last:border-0">
              <td className="py-1.5 pr-4 font-mono text-mono font-semibold whitespace-nowrap">
                {shortcut.keys}
              </td>
              <td className="py-1.5">{shortcut.action}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {extra ? <p className="mt-3 text-body-s text-ink-muted">{extra}</p> : null}
      <div className="mt-4 flex justify-end">
        <Button onClick={onClose}>Close</Button>
      </div>
    </Dialog>
  );
}

export function Flash({
  message,
  onClose,
  tone = "error",
}: {
  message: string | null;
  onClose: () => void;
  tone?: "error" | "info";
}) {
  if (!message) return null;
  const styles =
    tone === "info"
      ? "border-hairline-strong bg-surface-200 text-ink"
      : "border-critical-ink bg-critical-quiet text-critical-ink";
  return (
    <div
      role={tone === "info" ? "status" : "alert"}
      className={`flex items-start justify-between gap-4 rounded-sm border px-3 py-2 text-body-s ${styles}`}
    >
      <span>{message}</span>
      <button type="button" onClick={onClose} className="font-semibold" aria-label="Dismiss">
        ×
      </button>
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-2 text-body-s">
      <dt className="w-32 shrink-0 text-ink-subtle">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

/**
 * A page address. Machine-readable, so mono — and `brand-ink` because the system reserves red text
 * for a link or a live infringement, and this is both.
 */
export function ExternalLink({ url }: { url: string }) {
  if (!/^https?:\/\//i.test(url)) return <span className="font-mono text-mono break-all">{url}</span>;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="font-mono text-mono break-all text-brand-ink underline"
    >
      {url}
    </a>
  );
}
