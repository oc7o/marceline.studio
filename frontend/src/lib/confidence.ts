/** Detection confidence display for the reference queue (ADR 0015). Pure helpers, no React. */

export const BANDS = ["high", "review", "low"] as const;
export type Band = (typeof BANDS)[number];

export const DECIDED_BY = ["auto", "human"] as const;
export type DecidedByFilter = (typeof DECIDED_BY)[number];

/**
 * The confidence bands, on the design system's tokens. Named for what the band *means*, not for a
 * hue: `positive` is the blue-shifted teal (never a green — resolved must not depend on hue against
 * the red), `caution` is the band a person still has to look at, `neutral` is the one carrying no
 * claim. The percentage is always rendered beside the badge, so the colour only repeats it.
 */
export type BandTone = "positive" | "caution" | "neutral";

const TONES: Record<Band, BandTone> = { high: "positive", review: "caution", low: "neutral" };

const TONE_CLASSES: Record<BandTone, string> = {
  positive: "bg-positive-quiet text-positive-ink border-positive-ink",
  caution: "bg-caution-quiet text-caution-ink border-caution-ink",
  neutral: "bg-surface-200 text-ink-muted border-hairline-strong",
};

export function bandTone(band: Band): BandTone {
  return TONES[band];
}

export function bandClasses(band: Band): string {
  return TONE_CLASSES[bandTone(band)];
}

/** 0.7616 → "76%"; out-of-range values are clamped, unknown → "—". */
export function confidencePercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${Math.round(Math.min(Math.max(value, 0), 1) * 100)}%`;
}

/** Badge text: "high · 76%". */
export function bandLabel(band: Band, confidence: number): string {
  return `${band} · ${confidencePercent(confidence)}`;
}

export interface TimelineInput {
  timeline_score: number | null;
  consistent_frames: number;
  ordered_frames: number | null;
}

/** "timeline 0.99 · 9/9 frames in order", or "no frame order (poster only)". */
export function timelineText(match: TimelineInput): string {
  if (match.timeline_score === null) return "no frame order (poster only)";
  const frames =
    match.ordered_frames !== null
      ? `${match.consistent_frames}/${match.ordered_frames} frames in order`
      : `${match.consistent_frames} frames in order`;
  return `timeline ${match.timeline_score.toFixed(2)} · ${frames}`;
}

export interface NotesInput {
  loose_frames: number;
  common_frames: number;
  common_seconds: string[];
}

/** Notes for evidence admitted beyond the strict distance and for discounted common frames. */
export function evidenceNotes(match: NotesInput): string[] {
  const notes: string[] = [];
  if (match.loose_frames > 0) {
    const plural = match.loose_frames === 1 ? "frame" : "frames";
    notes.push(
      `${match.loose_frames} loose ${plural} (distance 32–40) admitted by timeline or cluster/mirror corroboration`,
    );
  }
  if (match.common_frames > 0) {
    const plural = match.common_frames === 1 ? "frame" : "frames";
    const moments = match.common_seconds.length ? ` at ${match.common_seconds.join(", ")}` : "";
    notes.push(
      `${match.common_frames} common ${plural}${moments} discounted (intro, watermark…): not evidence, lowers confidence`,
    );
  }
  return notes;
}

/** Signed contribution with two decimals: "+0.44", "−0.05", "0.00". */
export function formatContribution(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  if (rounded === 0) return "0.00";
  return `${rounded > 0 ? "+" : "−"}${Math.abs(rounded).toFixed(2)}`;
}

export interface SignalInput {
  key: string;
  label: string;
  value: string;
  weight: number | null;
  contribution: number;
}

export interface SignalRow {
  key: string;
  label: string;
  value: string;
  contribution: string;
  /** "of max 0.45" / "penalty up to 0.15"; empty for unknown components. */
  weight: string;
  /** Share of the component's maximum, 0..1, for a small bar; null without a weight. */
  share: number | null;
}

export function signalBreakdown(signals: SignalInput[]): SignalRow[] {
  return signals.map((signal) => {
    const { weight } = signal;
    const hasWeight = weight !== null && weight !== 0;
    return {
      key: signal.key,
      label: signal.label,
      value: signal.value,
      contribution: formatContribution(signal.contribution),
      weight: !hasWeight ? "" : weight < 0 ? `penalty up to ${Math.abs(weight).toFixed(2)}` : `of max ${weight.toFixed(2)}`,
      share: hasWeight ? Math.min(Math.max(signal.contribution / weight, 0), 1) : null,
    };
  });
}

/** Sum of the contributions before clamping to 0..1 (the stored confidence is clamped). */
export function signalTotal(signals: SignalInput[]): number {
  return Math.round(signals.reduce((sum, signal) => sum + signal.contribution, 0) * 10000) / 10000;
}

export function thresholdsText(thresholds: { high: number; review: number; calibrated: boolean }): string {
  return `high ≥ ${thresholds.high.toFixed(2)} (and ≥ 2 evidence frames) · review ≥ ${thresholds.review.toFixed(2)} · ${
    thresholds.calibrated ? "calibrated" : "defaults"
  }`;
}

// --- Band filter (URL state, `B` key) ---

/** Band filter from the URL: an array or a comma-separated string; unknown values dropped. */
export function parseBands(value: unknown): Band[] | undefined {
  const raw = Array.isArray(value) ? value : typeof value === "string" ? value.split(",") : [];
  const bands = BANDS.filter((band) => raw.includes(band));
  return bands.length ? bands : undefined;
}

export function parseDecidedBy(value: unknown): DecidedByFilter | undefined {
  return DECIDED_BY.find((option) => option === value);
}

/** Toggle one band chip; an empty selection means the default view (undefined). */
export function toggleBand(current: readonly Band[] | undefined, band: Band): Band[] | undefined {
  const selected = new Set(current ?? []);
  if (selected.has(band)) selected.delete(band);
  else selected.add(band);
  return parseBands([...selected]);
}

/** `B` cycles: default (high + review) → high → review → low → default. */
export function cycleBands(current: readonly Band[] | undefined): Band[] | undefined {
  const only = current?.length === 1 ? current[0] : undefined;
  if (only === undefined) return ["high"];
  const next = BANDS[BANDS.indexOf(only) + 1];
  return next ? [next] : undefined;
}

/** Short description of the active band filter for the top bar. */
export function bandFilterText(bands: readonly Band[] | undefined, includeLow: boolean): string {
  if (bands?.length) return `bands: ${bands.join(", ")}`;
  return includeLow ? "all bands" : "high + review (low hidden)";
}
