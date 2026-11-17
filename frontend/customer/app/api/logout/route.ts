/** `POST /app/api/logout` -> `POST /api/v1/app/auth/logout`. Django's cookie flush comes back. */
import { relay } from "../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/auth/logout", { method: "POST" });
}
