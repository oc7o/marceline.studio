/** Formatting helpers of the overview, originals and source candidate screens. Pure. */

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return Math.round(value).toLocaleString("en-US");
}

/** `part / whole` clamped to 0..1; 0 when the whole is empty. */
export function share(part: number, whole: number): number {
  if (!Number.isFinite(part) || !Number.isFinite(whole) || whole <= 0) return 0;
  return Math.min(Math.max(part / whole, 0), 1);
}

/** CSS width of a small bar. */
export function barWidth(part: number, whole: number): string {
  return `${Math.round(share(part, whole) * 1000) / 10}%`;
}

export type Tone = "ok" | "warn" | "bad";

/** Daily request budget: warn from 80 % used, bad when exhausted. */
export function budgetTone(used: number, limit: number): Tone {
  if (limit <= 0 || used >= limit) return "bad";
  return used / limit >= 0.8 ? "warn" : "ok";
}

/** A 0..1 rate with one decimal, e.g. `66.7%`; `n/a` without a denominator. */
export function rate(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "n/a";
  return `${(Math.round(value * 1000) / 10).toFixed(1)}%`;
}

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `$${value.toFixed(4)}`;
}

/** Coarse age like `5 min ago`; `—` for missing or unparsable timestamps. */
export function ago(value: string | null | undefined, now: Date): string {
  if (!value) return "—";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "—";
  const minutes = Math.max(0, Math.round((now.getTime() - then) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/** Binary byte size, one decimal from KB up: `1.2 GB`, `84 MB`, `512 B`; `—` when unknown. */
export function bytesLabel(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value < 0) return "—";
  let size = value;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < BYTE_UNITS.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const unit = BYTE_UNITS[unitIndex] ?? "B";
  return unitIndex === 0 ? `${Math.round(size)} ${unit}` : `${size.toFixed(1)} ${unit}`;
}

/** A plain duration like `5 min`, `2 h`, `3 d`; `—` for missing or unparsable values. */
export function durationLabel(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total} s`;
  const minutes = Math.round(total / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.round(hours / 24)} d`;
}

/** Load average tone against core count: warn at 1x cores, bad at 2x. */
export function loadTone(load: number | null | undefined, cores: number | null | undefined): Tone {
  if (load === null || load === undefined || !cores || cores <= 0) return "ok";
  if (load >= cores * 2) return "bad";
  return load >= cores ? "warn" : "ok";
}

export function robotsLabel(allows: boolean | null | undefined): string {
  if (allows === null || allows === undefined) return "not probed";
  return allows ? "allowed" : "disallowed";
}

const HOSTNAME = /^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$/;

/** A candidate's homepage as a plain https link; null for anything that is not a bare hostname. */
export function domainHref(domain: string): string | null {
  return HOSTNAME.test(domain) ? `https://${domain}/` : null;
}

/** POSIX shell single-quoting for copyable commands. */
export function shellQuote(value: string): string {
  return /^[A-Za-z0-9_./:=@-]+$/.test(value) ? value : `'${value.replaceAll("'", `'\\''`)}'`;
}

/** The CLI import of a creator original (import is CLI-only, ADR 0010). */
/** `--creator` is repeatable: the primary creator first, then co-performers (issue #64). */
export function addReferenceCommand(
  creators: readonly string[] = ["CREATOR"],
  file = "/path/to/original.mp4",
  title = "TITLE",
): string {
  const names = creators.length ? creators : ["CREATOR"];
  return [
    "cd backend && uv run python manage.py add_reference",
    ...names.map((name) => `--creator ${shellQuote(name)}`),
    `--file ${shellQuote(file)}`,
    `--title ${shellQuote(title)}`,
  ].join(" ");
}

export interface QuerySample {
  query: string;
  rank: number | null;
  url: string;
  title: string;
}

/** Search evidence as text lines: `#2 “query” → url · title`. Never rendered as links. */
export function queryLine(sample: QuerySample): string {
  const rank = sample.rank === null ? "#?" : `#${sample.rank}`;
  const title = sample.title ? ` · ${sample.title}` : "";
  return `${rank} “${sample.query}” → ${sample.url}${title}`;
}
