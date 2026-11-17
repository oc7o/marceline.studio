/**
 * `POST /app/api/verify-email/{token}` -> `POST /api/v1/app/auth/verify-email/{token}`
 * (ADR 0054, issue #149).
 *
 * The confirmation itself, as a **POST**: a GET is fetched by mail clients, scanners and link
 * previews, and an address confirmed by a scanner is a confirmation nobody made.
 */
import { isAccountToken, linkNotFound } from "../../../../lib/accountLink";
import { relay } from "../../../../lib/django";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ token: string }> },
): Promise<Response> {
  const { token } = await params;
  if (!isAccountToken(token)) return linkNotFound();
  return relay(request, `/api/v1/app/auth/verify-email/${token}`, { method: "POST" });
}
