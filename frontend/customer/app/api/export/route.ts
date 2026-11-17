/**
 * `GET /app/api/export` -> `GET /api/v1/app/export` (ADR 0045 amendment 2026-09-18, issue #85).
 *
 * The customer's own data export, as a file: a browser follows this link itself, like the
 * invoice PDF and the notice downloads. No path parameter, and nothing to validate beyond that —
 * the organisation comes from the session on the Django side, never from anything this server
 * could pass along.
 *
 * **A non-200 is not relayed** (issue #131). The export is throttled to one per rolling hour, so
 * a second click used to download `{"detail":"rate_limited"}` as a file, with nothing on the page
 * saying why — the same for the 404 an account without an organisation gets. Only a 200 leaves as
 * a download; anything else redirects to `/app/export`, which turns the status into one sentence
 * (`@shared/app/exportDownload`, `screens/ExportResultScreen.tsx`).
 */
import { exportProblemFor } from "@shared/app/exportDownload";

import { relay } from "../../../lib/django";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const response = await relay(request, "/api/v1/app/export", { method: "GET" });
  if (response.status === 200) return response;
  const problem = exportProblemFor(response.status);
  // The refusal's own body (`{"detail":"rate_limited"}`) is exactly what a customer used to
  // download; it is never read here, only drained so the connection closes cleanly.
  await response.body?.cancel();
  const url = new URL("/app/export", request.url);
  url.searchParams.set("problem", problem);
  return Response.redirect(url, 303);
}
