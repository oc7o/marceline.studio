/**
 * The CSRF double-submit, from the browser's side (ADR 0036).
 *
 * Django's check is unchanged: the `csrftoken` cookie has to come back as an `X-CSRFToken` header.
 * The BFF forwards both verbatim, so the token stays a secret between this browser and Django and
 * the Next server has nothing to substitute.
 *
 * Nothing but `document.cookie`, so an island can read it without a request. The cookie is set by
 * `GET /api/v1/app/auth/session`, which the sign-in island calls first (ADR 0023: the login POST
 * is refused without it).
 */
export function readCookie(name: string, cookies: string): string | null {
  for (const part of cookies.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return decodeURIComponent(rest.join("="));
  }
  return null;
}

export function csrfToken(): string | null {
  if (typeof document === "undefined") return null;
  return readCookie("csrftoken", document.cookie);
}
