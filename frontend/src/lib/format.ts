/** mm:ss like the admin (minutes are not wrapped into hours); "—" for unknown. */
export function clock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const whole = Math.max(0, Math.trunc(seconds));
  const minutes = Math.trunc(whole / 60);
  return `${String(minutes).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
}

export function signedSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const rounded = Math.round(seconds);
  return `${rounded > 0 ? "+" : ""}${rounded}s`;
}

export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toISOString().slice(0, 16).replace("T", " ");
}

/**
 * Everyone in an original as one line: "Sasha Meyer + Carla Bremer".
 * The API sends `creator_names` with the primary creator (rights holder) first; keep that order.
 */
export function creatorLabel(names: readonly string[] | null | undefined, fallback = "—"): string {
  const cleaned = (names ?? []).filter((name) => name.trim().length > 0);
  return cleaned.length ? cleaned.join(" + ") : fallback;
}

/** Page URLs come from crawled data: only http(s) may become links. */
export function safeHref(url: string): string | null {
  return /^https?:\/\//i.test(url) ? url : null;
}

export function openExternal(url: string): void {
  const href = safeHref(url);
  if (href) window.open(href, "_blank", "noopener,noreferrer");
}
