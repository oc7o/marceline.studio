/** `POST /app/api/data-requests/export/withdraw` -> take the open export request back (ADR 0056). */
import { relay } from "../../../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/data-requests/export/withdraw", { method: "POST" });
}
