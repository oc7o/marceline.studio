/**
 * `POST /app/api/uploads` -> `POST /api/v1/app/uploads` (issue #59, ADR 0044).
 *
 * Declares one upload: sizes, the title the customer typed, the cast and the rights declaration.
 * The file itself travels in the chunk route next to this one; the file's **name never travels at
 * all** — the browser uses it to pre-fill the title and nothing else.
 */
import { relay } from "../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/uploads", { method: "POST" });
}

export function GET(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/uploads", { method: "GET" });
}
