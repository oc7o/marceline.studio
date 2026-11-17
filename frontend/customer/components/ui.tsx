/**
 * The presentational primitives the customer screens use, as server components (ADR 0036).
 *
 * Every colour here is a design-system token (`app/globals.css`), never a literal: a component
 * picks a *role* — ground, ink, quiet status wash — and the theme decides the value, in light and
 * dark alike. Nothing in this file carries a word of its own; labels arrive as props, in the
 * customer's language, from `src/i18n` (frontend/src/app/CLAUDE.md "Wording rules").
 *
 * No `Button` with an `onClick`: a button that does something is an island of its own. No media of
 * any kind (issue #53). The focus ring is global (`:focus-visible` in `globals.css`), so nothing
 * here declares one and nothing here can forget one.
 */
import type { ReactNode } from "react";

/**
 * The Veil: the system's signature background, as three large translucent planes drifting off the
 * edges of the box they sit in, softened by `blur-veil`. Sculpture, not gradient — the shapes are
 * hard-edged rectangles before the blur, and they overlap so the intersections are the darkest and
 * the lightest part of the composition.
 *
 * The system's rules, all of them load-bearing here:
 * - **one per view**, and **behind chrome only** — a page header, a sign-in card, an empty state.
 *   Never behind a table or body copy, which is why this is never rendered by a list or a card
 *   that carries facts.
 * - **it does not animate**, at all. Nothing to do for `prefers-reduced-motion` because there is
 *   no motion to reduce.
 * - it is decoration: `aria-hidden`, `pointer-events-none`, and it draws nothing a reader needs.
 *
 * Put it inside a `relative isolate` box; `-z-10` then keeps it behind that box's own content
 * without escaping into the page. It renders no image (issue #53) — three coloured rectangles.
 */
export function Veil() {
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 -z-10 overflow-hidden">
      <span className="absolute -top-20 -left-16 h-56 w-80 rotate-12 bg-veil-brand blur-veil" />
      <span className="absolute -top-8 -right-12 h-48 w-72 -rotate-6 bg-veil-accent blur-veil" />
      <span className="absolute -bottom-24 left-1/4 h-40 w-96 rotate-3 bg-veil-ink blur-veil" />
    </div>
  );
}

/** A `<dl>` for the metadata blocks on a record. Its rows are `Field`s. */
export function DefinitionList({ children }: { children: ReactNode }) {
  return <dl className="flex flex-col gap-1">{children}</dl>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-2 text-body-s">
      <dt className="w-32 shrink-0 text-ink-muted">{label}</dt>
      <dd className="min-w-0 break-words text-ink">{children}</dd>
    </div>
  );
}

/**
 * A link that looks like a button. `primary` is the one brand-filled action in a view — the system
 * asks for red to be spent, not spread; everything else is `secondary` on a neutral ground.
 */
export function LinkButton({
  href,
  variant = "primary",
  block = false,
  children,
}: {
  href: string;
  variant?: "primary" | "secondary";
  /**
   * Full width up to `sm`, then back to its own size (issue #117). The one action a screen is
   * *for* is worth the whole width of a phone; a secondary action beside it is not, which is why
   * this is opt-in rather than tied to `variant`.
   */
  block?: boolean;
  children: ReactNode;
}) {
  const tone =
    variant === "primary"
      ? "border-brand bg-brand text-on-brand shadow-sm hover:bg-brand-hover"
      : "border-border-control bg-surface-100 text-ink hover:bg-surface-300";
  // `.tap` is the 44 px floor on a finger-driven browser; `globals.css` holds the rule and why.
  // Width only, never a second `display`: `.tap` is unlayered CSS and would win over one anyway.
  const width = block ? "w-full justify-center sm:w-auto sm:justify-start" : "";
  return (
    <a
      href={href}
      className={`tap inline-flex items-center gap-1 rounded-sm border px-3 py-1.5 text-button transition-colors duration-120 ease-standard ${tone} ${width}`}
    >
      {children}
    </a>
  );
}

/** One number with what it counts. The number is mono (`metric`), so digits line up between tiles. */
export function StatTile({
  label,
  value,
  hint,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-md border border-hairline bg-surface-100 p-4 shadow-sm">
      <span className="text-overline text-ink-muted uppercase">{label}</span>
      <span className="font-mono text-metric text-ink">{value}</span>
      {hint === undefined ? null : <span className="text-caption text-ink-subtle">{hint}</span>}
    </div>
  );
}

/**
 * Nothing to show, said in the serif voice with at most one thing to do about it. Dashed, so an
 * empty list reads as empty rather than as a card that failed to load.
 */
export function EmptyState({
  title,
  body,
  next,
  action,
  veil = false,
}: {
  /**
   * The headline, in the display serif. Optional: some of these states have one sentence and
   * nothing else, and a headline would have to be invented — the words of this product are fixed
   * (frontend/src/app/CLAUDE.md "Wording rules"), so the sentence goes in `body` and there is no headline.
   */
  title?: string;
  body?: ReactNode;
  /**
   * **What happens next** (issue #117): the sentence that turns "there is nothing here" into
   * "here is what this screen is waiting for". A new creator meets four of these before the
   * product has anything to show, and an empty screen that explains nothing reads as a product
   * that is not working. It is a separate slot rather than more `body` so that it is always the
   * last paragraph, always quieter, and so a test can find it.
   *
   * It says nothing about **when** — `wording.test.ts` bans a promised duration, and this is
   * exactly the sentence where one would be tempting.
   */
  next?: ReactNode;
  action?: ReactNode;
  /**
   * Draw the Veil behind it. Opt-in, and never defaulted: the system allows one Veil per view, and
   * only the screen knows whether this empty state *is* the view or sits under a header that
   * already has one.
   */
  veil?: boolean;
}) {
  return (
    <div className="relative isolate flex flex-col items-start gap-2 overflow-hidden rounded-md border border-dashed border-hairline-strong bg-surface-100 p-4 sm:p-6">
      {veil ? <Veil /> : null}
      {title === undefined ? null : <p className="font-display text-display-m text-ink">{title}</p>}
      {body === undefined ? null : <p className="max-w-[68ch] text-body text-ink-muted">{body}</p>}
      {next === undefined ? null : (
        <p data-empty-next="" className="max-w-[68ch] text-body-s text-ink-subtle">
          {next}
        </p>
      )}
      {action === undefined ? null : <div className="mt-2 w-full sm:w-auto">{action}</div>}
    </div>
  );
}

/** The strip a list's filters and switches sit in. Raised chrome, never a card. */
export function Toolbar({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div
      role="group"
      aria-label={label}
      className="flex flex-wrap items-center gap-2 rounded-sm border border-hairline bg-surface-200 px-3 py-2 text-body-s text-ink"
    >
      {children}
    </div>
  );
}

/** One end of a `Pagination`: a link when there is a page there, a dead label when there is not. */
export type PageLink = { label: string; href: string } | { label: string; href?: undefined };

function PageStep({ step, rel }: { step: PageLink; rel: "prev" | "next" }) {
  // `.tap` on both ends, the dead one included: the two steps must stay the same height.
  const shape = "tap rounded-sm border px-3 py-1.5 text-button";
  if (step.href === undefined) {
    return (
      <span aria-disabled="true" className={`${shape} border-hairline text-ink-subtle opacity-45`}>
        {step.label}
      </span>
    );
  }
  return (
    <a
      href={step.href}
      rel={rel}
      className={`${shape} border-border-control bg-surface-100 text-ink transition-colors duration-120 ease-standard hover:bg-surface-300`}
    >
      {step.label}
    </a>
  );
}

/**
 * Previous / next over a server-rendered list: plain links, because the page is the server's and
 * following one *is* the navigation. `status` is the position in words ("21–40 of 57"), built by
 * the screen from the customer's own resources.
 */
export function Pagination({
  label,
  status,
  previous,
  next,
}: {
  label: string;
  status: string;
  previous: PageLink;
  next: PageLink;
}) {
  return (
    <nav aria-label={label} className="flex flex-wrap items-center justify-between gap-3">
      <PageStep step={previous} rel="prev" />
      <span className="text-body-s text-ink-muted">{status}</span>
      <PageStep step={next} rel="next" />
    </nav>
  );
}

/**
 * A neutral operational notice: what the product is doing, or something about this account worth
 * knowing. **Not** legal text — a notice that makes a legal statement is the customer's own and is
 * rendered verbatim from the server, never composed here.
 *
 * `automated` is the violet one: the system's rule is that anything the machine decided reads
 * differently from anything a person decided.
 */
export type BannerTone = "neutral" | "caution" | "critical" | "automated";

const BANNER_TONES: Record<BannerTone, string> = {
  neutral: "border-hairline bg-surface-200 text-ink",
  caution: "border-caution-ink/40 bg-caution-quiet text-caution-ink",
  critical: "border-critical-ink/40 bg-critical-quiet text-critical-ink",
  automated: "border-accent/40 bg-accent-quiet text-accent",
};

export function Banner({
  tone = "neutral",
  role,
  children,
}: {
  tone?: BannerTone;
  /** "status" for something that changed, "note" for standing context; omit for plain prose. */
  role?: "status" | "note" | "alert";
  children: ReactNode;
}) {
  return (
    <div role={role} className={`rounded-md border p-3 text-body-s ${BANNER_TONES[tone]}`}>
      {children}
    </div>
  );
}

/**
 * The placeholder a row leaves while the server is still fetching it. Announced, because a sighted
 * reader sees the shimmer and a screen-reader user would otherwise hear nothing at all.
 *
 * The shimmer is opacity only, and `prefers-reduced-motion` stops it dead (`globals.css`); the bars
 * keep their ground either way, so the row still reads as "something is coming".
 */
export function SkeletonRow({ label, lines = 2 }: { label: string; lines?: number }) {
  const widths = ["w-1/2", "w-5/6", "w-2/3", "w-3/4"];
  return (
    <div
      role="status"
      aria-label={label}
      className="flex flex-col gap-2 rounded-md border border-hairline bg-surface-100 p-4"
    >
      {Array.from({ length: Math.max(1, lines) }, (_, index) => (
        <span
          key={index}
          aria-hidden="true"
          className={`h-3 animate-shimmer rounded-sm bg-surface-300 ${widths[index % widths.length]}`}
        />
      ))}
    </div>
  );
}
