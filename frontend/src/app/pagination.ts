/**
 * Server-side pagination over a list already in hand (issue #134).
 *
 * `GET /api/v1/app/originals`, `/findings` and `/removals` take no query parameters at all
 * (`schema.d.ts`: `query?: never`) — there is no cursor to ask for a page of. Adding one is an
 * ADR-sized decision (production reads customer data from a signed bundle, ADR 0028, so a cursor
 * changes what goes into the bundle, not only what a screen requests) and is not what this module
 * does. This only caps what a screen **renders**: the whole list Django already sent is still the
 * whole list this server received: a studio with 500 originals still costs one full read from
 * Django on every visit to `/app`. What changes is the HTML a browser gets, which is where issue
 * #134 measured the problem (199 KB for 45 originals, unbounded).
 */

export const PAGE_SIZE = 20;

export interface Page<T> {
  readonly items: readonly T[];
  /** 1-based, clamped to a real page even when the requested one was not. */
  readonly page: number;
  readonly pageCount: number;
  /** 1-based, both inclusive; 0 and 0 when the list is empty. */
  readonly start: number;
  readonly end: number;
  readonly total: number;
}

/** Slices `items` to one page. `page` is clamped to `[1, pageCount]`, never out of range. */
export function paginate<T>(items: readonly T[], page: number, pageSize: number = PAGE_SIZE): Page<T> {
  const total = items.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.min(Math.max(1, Math.trunc(page)), pageCount);
  const start = (current - 1) * pageSize;
  const end = Math.min(start + pageSize, total);
  return {
    items: items.slice(start, end),
    page: current,
    pageCount,
    start: total === 0 ? 0 : start + 1,
    end,
    total,
  };
}

/** The `?page=` query value: a whole number of at least 1, or 1 for anything else — never NaN,
 *  never negative, never a decimal a customer or a stale bookmark could send. */
export function pageFromParam(value: string | readonly string[] | undefined): number {
  const raw = Array.isArray(value) ? value[0] : value;
  const parsed = raw === undefined ? Number.NaN : Number.parseInt(raw, 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

/** The link for one pagination step: the bare path for page 1, `?page=N` otherwise, or no link
 *  at all (`null`) when that step does not exist. */
export function pageHref(basePath: string, page: number | null): string | undefined {
  if (page === null) return undefined;
  return page === 1 ? basePath : `${basePath}?page=${page}`;
}

/**
 * One end of a `Pagination` (`components/ui.tsx`): a link when there is a page there, a dead
 * label when there is not. Built here rather than as `{ label, href }` at the call site, because
 * `PageLink` is a union that only accepts `href` present-and-a-string or absent-and-never-a-
 * string — a `string | undefined` value in the `href` key does not satisfy either branch, which
 * `pageHref` returning `undefined` for "no such page" would otherwise produce.
 */
export function pageLink(
  label: string,
  href: string | undefined,
): { label: string; href: string } | { label: string } {
  return href === undefined ? { label } : { label, href };
}
