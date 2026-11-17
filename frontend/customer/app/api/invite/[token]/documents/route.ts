/**
 * `POST /app/api/invite/{token}/documents` -> `POST /api/v1/app/invite/{token}/documents` (ADR 0052).
 *
 * One handler per step, each naming its Django path literally. The token is checked against the
 * shape the server generates before it reaches the template string (`lib/inviteToken.ts`), and a
 * token of another shape gets the same neutral 404 as one that does not work.
 */
import { relay } from "../../../../../lib/django";
import { inviteNotFound, isInviteToken } from "../../../../../lib/inviteToken";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ token: string }> },
): Promise<Response> {
  const { token } = await params;
  if (!isInviteToken(token)) return inviteNotFound();
  return relay(request, `/api/v1/app/invite/${token}/documents`, { method: "POST" });
}
