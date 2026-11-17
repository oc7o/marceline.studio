/** `POST /app/api/data-requests/export` -> ask for a copy of this account's data (ADR 0056). */
import { relay } from "../../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/data-requests/export", { method: "POST" });
}
