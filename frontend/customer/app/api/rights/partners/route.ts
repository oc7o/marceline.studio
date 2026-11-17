/** `POST /app/api/rights/partners` -> adding a licensed partner (ADR 0034). */
import { relay } from "../../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/rights/partners", { method: "POST" });
}
