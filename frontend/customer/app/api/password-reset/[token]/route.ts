/**
 * `POST /app/api/password-reset/{token}` -> `POST /api/v1/app/auth/password-reset/{token}`
 * (ADR 0054, issue #149).
 *
 * The token's shape is checked before it reaches the template string (`lib/accountLink.ts`), and a
 * token of another shape gets the same neutral 404 as one that does not work.
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
  return relay(request, `/api/v1/app/auth/password-reset/${token}`, { method: "POST" });
}
