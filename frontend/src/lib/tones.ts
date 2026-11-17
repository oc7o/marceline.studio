/**
 * The badge tones of the staff SPA, on the design system's tokens (`src/index.css`).
 *
 * They live in `lib/` rather than in `components/ui.tsx` because the pure display helpers
 * (`copyExtent.ts`, `confidence.ts`) decide a tone without importing React, and because naming them
 * once is what keeps the system's three rules from being re-argued per screen:
 *
 * - **Automation is violet.** `accent` is what the machine decided — a PDQ match, a cluster
 *   proposal, a `decided_by=auto` row, a copy extent the matcher derived. A human decision never
 *   takes it.
 * - **Spend the red.** `critical` is a live problem: a page still online, a rejected claim, a
 *   blocked page. Not every negative number.
 * - **Colour never works alone.** Everything that takes one of these also renders its state as a
 *   word, so the ground only repeats what the label already says.
 *
 * The customer app has its own copy of this idea (`frontend/customer/lib/tones.ts`), keyed by its
 * own shared types. The two never import each other: the words and the codes are shared, the class
 * strings belong to the app that draws them.
 */
export type BadgeTone = "neutral" | "accent" | "caution" | "positive" | "critical";

export const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: "bg-surface-200 text-ink-muted",
  accent: "bg-accent-quiet text-accent",
  caution: "bg-caution-quiet text-caution-ink",
  positive: "bg-positive-quiet text-positive-ink",
  critical: "bg-critical-quiet text-critical-ink",
};
