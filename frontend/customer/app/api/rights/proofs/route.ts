/**
 * `POST /app/api/rights/proofs` -> uploading a rights proof document (ADR 0034).
 *
 * `multipart/form-data`, streamed through with the browser's own boundary. Proof documents are
 * development-only for now, so on production this is the 404 Django already answers; the screen
 * renders that as "not shown here yet". Nothing is ever offered back for download.
 */
import { relay } from "../../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/rights/proofs", { method: "POST" });
}
