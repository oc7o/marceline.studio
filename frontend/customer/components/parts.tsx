/**
 * The building blocks the customer screens share (ported from the Vite SPA's `src/app/parts.tsx`,
 * ADR 0036): badges, cards, dates and the one way a matched page address is shown.
 *
 * All **server** components, and all synchronous: the words arrive as an `i18n` prop rather than
 * from a hook, so every screen below renders on the server with no JavaScript behind it and can be
 * rendered straight to a string in a test.
 *
 * Every badge carries its words, never colour alone — and every colour is a design-system token
 * (`app/globals.css`), so the same badge reads correctly in both themes.
 */
import type { ReactNode } from "react";

import type { OriginalState, Whereabouts } from "@shared/app/caseFile";

import type { Words } from "../lib/words";
import { LinkButton } from "./ui";

/** A date as the customer's language writes it, with the time: "16 Sept 2026, 14:05". */
export function formatDate(value: string, language: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(language, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

/**
 * A day the API sent as "2026-09-17", with no time: "17 Sept 2026". Read as a calendar date in UTC
 * so no time zone can move it to the day before.
 */
export function formatDay(value: string, language: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return value;
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return new Intl.DateTimeFormat(language, { dateStyle: "medium", timeZone: "UTC" }).format(date);
}

/**
 * A failed read: no server detail, no status dump — a sentence and a way to try again. The retry
 * is a plain link to the same page, because the page is rendered by the server: reloading it *is*
 * the retry, and it needs no JavaScript.
 */
export function LoadError({ i18n, href }: { i18n: Words; href: string }) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-3 rounded-md border border-critical-ink/40 bg-critical-quiet p-3 text-body-s text-critical-ink"
    >
      <span>{i18n.t("common.loadFailed")}</span>
      <LinkButton href={href} variant="secondary">
        {i18n.t("common.retry")}
      </LinkButton>
    </div>
  );
}

export function Card({ children, labelledBy }: { children: ReactNode; labelledBy?: string }) {
  return (
    <section
      aria-labelledby={labelledBy}
      className="rounded-md border border-hairline bg-surface-100 p-4 text-ink"
    >
      {children}
    </section>
  );
}

/**
 * A status badge's shape — the system's `StatusPill`, which is the one place `radius-pill` is used.
 * `whitespace-nowrap`: a label reads as one unit, never split over lines.
 */
export const BADGE =
  "inline-block rounded-full px-2 py-0.5 text-body-s font-medium whitespace-nowrap";

/**
 * Title-and-status rows share these classes (through `TitleRow`), so every list lines up the same
 * way. Wider than 380 px: one row, the title flexible, the badge right-aligned and never wrapped.
 * Narrower: the badge is a deliberate second row rather than an accidental wrap — German labels
 * ("Hier entfernt · Vorgang noch offen") are the long case.
 */
export const TITLE_ROW = {
  row: "flex flex-col gap-1 min-[381px]:flex-row min-[381px]:items-start min-[381px]:justify-between min-[381px]:gap-3",
  title: "min-w-0 min-[381px]:flex-1",
  badge: "shrink-0 self-start whitespace-nowrap",
  /** Two lines, not one: originals often share a long prefix, and one line would cut what tells
   *  them apart, while two keep the cards' heights even. */
  clamp: "line-clamp-2 break-words",
} as const;

export function TitleRow({ title, badge }: { title: ReactNode; badge: ReactNode }) {
  return (
    <div data-title-row="" className={TITLE_ROW.row}>
      <div className={TITLE_ROW.title}>{title}</div>
      <div className={TITLE_ROW.badge}>{badge}</div>
    </div>
  );
}

/**
 * Red means found, the teal means gone, neutral means nothing to report. The teal is deliberately
 * not a green (the system's rule): resolved must not depend on hue against the red.
 */
const STATE_TONES: Record<OriginalState, string> = {
  found_online: "bg-critical-quiet text-critical-ink",
  found_removed: "bg-positive-quiet text-positive-ink",
  nothing_found: "bg-surface-300 text-ink-muted",
};

export function StateBadge({ state, i18n }: { state: OriginalState; i18n: Words }) {
  return <span className={`${BADGE} ${STATE_TONES[state]}`}>{i18n.t(`states.${state}`)}</span>;
}

const WHEREABOUTS_TONES: Record<Whereabouts, string> = {
  online: "bg-critical-quiet text-critical-ink",
  gone: "bg-positive-quiet text-positive-ink",
  unknown: "bg-surface-300 text-ink-muted",
};

export function WhereaboutsBadge({
  whereabouts,
  i18n,
}: {
  whereabouts: Whereabouts;
  i18n: Words;
}) {
  return (
    <span className={`${BADGE} ${WHEREABOUTS_TONES[whereabouts]}`}>
      {i18n.t(`finding.whereabouts.${whereabouts}`)}
    </span>
  );
}

/** http(s) only; anything else stays text rather than becoming a link. */
export function safeHref(url: string): string | null {
  return /^https?:\/\//i.test(url) ? url : null;
}

/**
 * A matched page address: **text**, plus one explicit link that opens it in a new tab without a
 * referrer. Never an embed, a preview, a favicon or anything else the browser would fetch by
 * itself.
 */
export function PageAddress({ url, i18n }: { url: string; i18n: Words }) {
  const href = safeHref(url);
  return (
    <div className="flex min-w-0 flex-col gap-1">
      {/* An address is evidence, so its well stays square (`radius-none`) and inset. */}
      <span className="rounded-none border border-hairline bg-surface-inset px-2 py-1 font-mono text-mono-s break-all text-ink">
        {url}
      </span>
      {href ? (
        <>
          <a
            href={href}
            target="_blank"
            rel="noreferrer noopener"
            className="w-fit text-body-s text-brand-ink underline"
          >
            {i18n.t("caseFile.where.open")}
          </a>
          <span className="text-caption text-ink-subtle">{i18n.t("caseFile.where.openWarning")}</span>
        </>
      ) : null}
    </div>
  );
}

/**
 * The state of one step in a record's timeline. `unavailable` is not "upcoming": it is a step
 * nothing we know of will produce, and the screen says why (`caseFile.reasons.*`).
 */
export type TimelineStatus = "done" | "pending" | "unavailable";

export interface TimelineItem {
  /** React's key, and the `data-step` a test reads. The step's code, not a word. */
  step: string;
  status: TimelineStatus;
  /** What the step is, already in the customer's words. */
  title: ReactNode;
  /** The status line under it: the state, its date, who acted. */
  meta?: ReactNode;
  /** One more line, when there is something to add — a reason, or a copy that came back. */
  note?: ReactNode;
}

const STATUS_MARKER: Record<TimelineStatus, string> = {
  done: "border-positive-ink bg-positive-ink",
  pending: "border-border-control bg-surface-100",
  unavailable: "border-dashed border-hairline-strong bg-surface-300",
};

/**
 * The steps of a record, in order, as an ordered list. Colour never carries the state on its own:
 * every entry names its status in `meta`, and the marker only repeats it.
 */
export function Timeline({ items, labelledBy }: { items: readonly TimelineItem[]; labelledBy?: string }) {
  return (
    <ol aria-labelledby={labelledBy} className="flex flex-col">
      {items.map((item) => (
        <li
          key={item.step}
          data-step={item.step}
          data-status={item.status}
          className="flex gap-3 pb-3 last:pb-0"
        >
          <span
            aria-hidden="true"
            className={`mt-1 h-3 w-3 shrink-0 rounded-full border-2 ${STATUS_MARKER[item.status]}`}
          />
          <div className="min-w-0 text-body-s">
            <div className={item.status === "done" ? "font-medium text-ink" : "text-ink-muted"}>
              {item.title}
            </div>
            {item.meta === undefined ? null : (
              <div className="text-caption text-ink-muted">{item.meta}</div>
            )}
            {item.note === undefined ? null : (
              <div className="text-caption text-ink-subtle">{item.note}</div>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
