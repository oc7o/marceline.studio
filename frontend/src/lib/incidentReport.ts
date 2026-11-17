/**
 * Safety incident report drafts (issue #66): the words the staff screen puts around the draft the
 * API renders. The draft text itself comes from the server in German and English and is shown
 * verbatim — never rebuilt here, so what is copied is exactly what was reviewed.
 */

export type DraftLanguage = "de" | "en";

export interface DraftText {
  language: DraftLanguage;
  subject: string;
  body: string;
}

export const LANGUAGE_LABELS: Record<DraftLanguage, string> = { de: "Deutsch", en: "English" };

export function draftFor<T extends DraftText>(drafts: readonly T[], language: DraftLanguage): T | null {
  return drafts.find((draft) => draft.language === language) ?? null;
}

/** Subject and body as one block, for forms with a single free-text field. */
export function fullText(draft: DraftText): string {
  return `${draft.subject}\n\n${draft.body}`;
}

export const MEDIA_FETCH_LABELS: Record<string, string> = {
  none: "No media of this case was ever requested — the draft says so.",
  fetched:
    "Media of at least one URL was requested (for hashing) before the flag — the draft says so truthfully.",
  unknown: "A page row is gone, so whether media was requested can no longer be told — the draft says so.",
};

export function mediaFetchLabel(value: string): string {
  return MEDIA_FETCH_LABELS[value] ?? `Media fetch status: ${value}`;
}

export const EVIDENCE_LABELS: Record<string, string> = {
  origin: "the flagged page",
  same_url: "the flagged page (seen again)",
  term_change: "the flagged page (re-flagged)",
  mirror: "same content: mirror poster path",
  sha256: "same content: identical media bytes",
  cluster: "same content: video cluster",
  pdq: "same content: image fingerprint kept on this case",
  manual: "attached by staff",
};

export function evidenceLabel(value: string): string {
  return EVIDENCE_LABELS[value] ?? value;
}

/** `datetime-local` input value (local time, minutes) for a date. */
export function toLocalInput(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}`;
}

export interface ReportedForm {
  reference: string;
  via: string;
  reportCategory: string;
  reportedAt: string; // datetime-local value; empty = now
  language: DraftLanguage;
}

/** Request body for `POST /review/safety/incidents/{id}/reported`; null when the date is invalid. */
export function reportedBody(form: ReportedForm): {
  reference: string;
  via: string;
  report_category: string;
  reported_at: string | null;
  language: DraftLanguage;
} | null {
  let reportedAt: string | null = null;
  if (form.reportedAt.trim()) {
    const parsed = new Date(form.reportedAt);
    if (Number.isNaN(parsed.getTime())) return null;
    reportedAt = parsed.toISOString();
  }
  return {
    reference: form.reference.trim(),
    via: form.via.trim(),
    report_category: form.reportCategory.trim(),
    reported_at: reportedAt,
    language: form.language,
  };
}
