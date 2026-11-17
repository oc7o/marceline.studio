/**
 * `POST /app/api/login` -> `POST /api/v1/app/auth/login` (ADR 0023).
 *
 * Django creates the session — hashing, throttle, lockout and one answer for every failure are
 * unchanged, and this server holds no password and no session of its own. Its answer, its
 * `Set-Cookie` and its `Retry-After` go back untouched; the words are the browser's job
 * (`@shared/app/signInError`).
 */
import { relay } from "../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/auth/login", { method: "POST" });
}
