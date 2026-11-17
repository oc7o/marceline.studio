/**
 * `POST /app/api/data-requests/deletion/withdraw` -> take the open deletion request back (ADR 0056).
 *
 * Its own path rather than a `[kind]` parameter, so `lib/routes.test.ts`'s "every write an island
 * sends has a handler" really checks this screen: a computed segment would match anything.
 */
import { relay } from "../../../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/data-requests/deletion/withdraw", { method: "POST" });
}
