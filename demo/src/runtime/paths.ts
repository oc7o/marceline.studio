/** The app lives under `/app`, as in production (`basePath` in `frontend/customer/next.config.ts`). */
export const BASE_PATH = "/app";

/** A router path (`/removals`) as an address (`/app/removals`). */
export function withBase(href: string): string {
  if (!href.startsWith("/")) return href;
  return href === "/" ? BASE_PATH : `${BASE_PATH}${href}`;
}

/** An address as a router path, or null when it is not inside the app. */
export function withoutBase(pathname: string): string | null {
  if (pathname !== BASE_PATH && !pathname.startsWith(`${BASE_PATH}/`)) return null;
  const rest = pathname.slice(BASE_PATH.length).replace(/\/+$/, "");
  return rest === "" ? "/" : rest;
}

export interface RoutePattern<T> {
  segments: string[];
  value: T;
}

/** `…/app/(group)/originals/[originalId]/page.tsx` as the segments of the address it answers. */
export function routeSegments(file: string, root: string, leaf: string): string[] {
  const inner = file.slice(file.indexOf(root) + root.length, file.length - leaf.length);
  return inner.split("/").filter((segment) => segment !== "" && !/^\(.*\)$/.test(segment));
}

/** The pattern an address matches; a literal segment beats a `[param]` one, as in Next. */
export function matchRoute<T>(
  patterns: readonly RoutePattern<T>[],
  pathname: string,
): { value: T; params: Record<string, string> } | null {
  const parts = pathname.split("/").filter((part) => part !== "");
  let best: { value: T; params: Record<string, string>; literals: number } | null = null;
  for (const pattern of patterns) {
    if (pattern.segments.length !== parts.length) continue;
    const params: Record<string, string> = {};
    let literals = 0;
    const fits = pattern.segments.every((segment, index) => {
      const part = parts[index] ?? "";
      const param = /^\[(\w+)\]$/.exec(segment);
      if (param?.[1]) {
        params[param[1]] = decodeURIComponent(part);
        return true;
      }
      literals += 1;
      return segment === part;
    });
    if (fits && (best === null || literals > best.literals)) best = { value: pattern.value, params, literals };
  }
  return best;
}
