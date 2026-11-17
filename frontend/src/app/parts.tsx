/**
 * Small building blocks the customer data screens share: query states, badges, dates and the one
 * way a pirate page address is shown.
 *
 * Every badge carries its words, never colour alone. Every query renders loading, error and — for a
 * 404 — "not found", which is all a 404 may say: an original that does not exist and an original
 * that belongs to another organisation are the same answer (ADR 0021).
 */
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { Button } from "../components/ui";
import { safeHref } from "../lib/format";
import { useLanguage } from "../i18n";
import type { OriginalState, Whereabouts } from "./caseFile";

/** A date as the customer's language writes it, with the time: "16 Sept 2026, 14:05". */
export function formatDate(value: string, language: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(language, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function useFormatDate(): (value: string) => string {
  const [language] = useLanguage();
  return (value: string) => formatDate(value, language);
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

export function useFormatDay(): (value: string) => string {
  const [language] = useLanguage();
  return (value: string) => formatDay(value, language);
}

export function Loading() {
  const { t } = useTranslation();
  return (
    <p role="status" className="text-slate-500">
      {t("common.loading")}
    </p>
  );
}

export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** A failed query: no server detail, no status dump — a sentence and a way to try again. */
export function LoadError({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 rounded border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
      <span>{t("common.loadFailed")}</span>
      <Button onClick={onRetry}>{t("common.retry")}</Button>
    </div>
  );
}

export function NotFound({ what }: { what: "original" | "caseFile" }) {
  const { t } = useTranslation();
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4">
      <h1 className="text-lg font-semibold">{t("notFound.heading")}</h1>
      <p className="mt-1 text-sm text-slate-600">
        {what === "original" ? t("notFound.original") : t("notFound.caseFile")}
      </p>
      <Link to="/app" className="mt-3 inline-block text-sm text-sky-800 underline">
        {t("notFound.back")}
      </Link>
    </section>
  );
}

export function Card({ children, labelledBy }: { children: ReactNode; labelledBy?: string }) {
  return (
    <section aria-labelledby={labelledBy} className="rounded-lg border border-slate-200 bg-white p-4">
      {children}
    </section>
  );
}

/** A status badge's shape. `whitespace-nowrap`: a label reads as one unit, never split over lines. */
export const BADGE = "inline-block rounded px-2 py-0.5 text-xs font-semibold whitespace-nowrap";

/**
 * Title-and-status rows share these classes (through `TitleRow`), so every list lines up the same way.
 *
 * - Wider than 380 px: one row. The title takes the flexible space (`flex-1 min-w-0`); the badge sits
 *   right-aligned at the top and never shrinks or wraps (`shrink-0 whitespace-nowrap`).
 * - 380 px and narrower (small phones): the badge is a deliberate second row, left-aligned under the
 *   title, rather than an accidental wrap. German labels ("Hier entfernt · Fall noch offen") are the
 *   long case.
 */
export const TITLE_ROW = {
  row: "flex flex-col gap-1 min-[381px]:flex-row min-[381px]:items-start min-[381px]:justify-between min-[381px]:gap-3",
  title: "min-w-0 min-[381px]:flex-1",
  badge: "shrink-0 self-start whitespace-nowrap",
  /**
   * List titles clamp to two lines, the full title in `title`. Two, not one: originals often share a
   * long prefix (a series, "Part 1" / "Part 2"), and one line at phone width would cut exactly the
   * part that tells them apart; two lines still keep the cards' heights even.
   */
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

const STATE_TONES: Record<OriginalState, string> = {
  found_online: "bg-rose-100 text-rose-900",
  found_removed: "bg-emerald-100 text-emerald-900",
  nothing_found: "bg-slate-100 text-slate-700",
};

export function StateBadge({ state }: { state: OriginalState }) {
  const { t } = useTranslation();
  return (
    <span className={`${BADGE} ${STATE_TONES[state]}`}>
      {t(`states.${state}`)}
    </span>
  );
}

const WHEREABOUTS_TONES: Record<Whereabouts, string> = {
  online: "bg-rose-100 text-rose-900",
  gone: "bg-emerald-100 text-emerald-900",
  unknown: "bg-slate-100 text-slate-700",
};

export function WhereaboutsBadge({ whereabouts }: { whereabouts: Whereabouts }) {
  const { t } = useTranslation();
  return (
    <span className={`${BADGE} ${WHEREABOUTS_TONES[whereabouts]}`}>
      {t(`finding.whereabouts.${whereabouts}`)}
    </span>
  );
}

/**
 * A pirate page address: **text**, plus one explicit link that opens it in a new tab without a
 * referrer. Never an embed, a preview, a favicon or anything else the browser would fetch by
 * itself. Only http(s) addresses become links; anything else stays text.
 */
export function PageAddress({ url }: { url: string }) {
  const { t } = useTranslation();
  const href = safeHref(url);
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="font-mono text-xs break-all text-slate-800">{url}</span>
      {href ? (
        <>
          <a href={href} target="_blank" rel="noreferrer noopener" className="w-fit text-sm text-sky-800 underline">
            {t("caseFile.where.open")}
          </a>
          <span className="text-xs text-slate-500">{t("caseFile.where.openWarning")}</span>
        </>
      ) : null}
    </div>
  );
}
