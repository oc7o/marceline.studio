/**
 * `GET /app/api/session` -> `GET /api/v1/app/auth/session` (ADR 0023).
 *
 * The only customer endpoint that needs no session, and the only thing that sets the `csrftoken`
 * cookie the login POST has to echo — which is why the relay passes `Set-Cookie` back.
 */
import { relay } from "../../../lib/django";

export const dynamic = "force-dynamic";

export function GET(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/auth/session", { method: "GET" });
}
