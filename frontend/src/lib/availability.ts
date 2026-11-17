/** Page availability as the crawler last saw it. Pure formatting, no clock of its own. */

export type AvailabilityTone = "online" | "removed" | "unknown";

export interface AvailabilityInfo {
  tone: AvailabilityTone;
  label: string;
  checked: string;
}

const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;

export function availabilityTone(availability: string): AvailabilityTone {
  if (availability === "playable") return "online";
  if (availability === "removed") return "removed";
  return "unknown";
}

/** "just now", "12 min ago", "3 h ago", "2 d ago"; null for missing or unreadable timestamps. */
export function relativeAge(value: string | null | undefined, now: Date): string | null {
  if (!value) return null;
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return null;
  const elapsed = Math.max(0, now.getTime() - then);
  if (elapsed < 60_000) return "just now";
  if (elapsed < HOUR) return `${Math.floor(elapsed / 60_000)} min ago`;
  if (elapsed < DAY) return `${Math.floor(elapsed / HOUR)} h ago`;
  return `${Math.floor(elapsed / DAY)} d ago`;
}

export function availabilityInfo(
  page: { availability: string; availability_checked_at: string | null },
  now: Date,
): AvailabilityInfo {
  const tone = availabilityTone(page.availability);
  const age = relativeAge(page.availability_checked_at, now);
  return { tone, label: tone, checked: age ? `checked ${age}` : "never checked" };
}

export const isRemoved = (page: { availability: string }) => page.availability === "removed";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** Toast after a recheck request: the API only queues tasks, the crawler visits them later. */
export function recheckMessage(result: { queued: number[]; in_progress: number[]; skipped: number[] }): string {
  const parts = [`Queued ${plural(result.queued.length, "page")} — results appear after the crawler visits them`];
  if (result.in_progress.length) parts.push(`${plural(result.in_progress.length, "page")} already being checked`);
  if (result.skipped.length) parts.push(`${plural(result.skipped.length, "page")} skipped (site not enabled)`);
  return `${parts.join("; ")}.`;
}
