/**
 * `GET /app/api/invoices/{inv_…}/pdf` -> the stored invoice PDF (ADR 0032).
 *
 * The one binary a customer downloads, and the one read that does not go through a server
 * component: a browser follows this link itself. It is relayed like every other call so the
 * browser never needs an address on the Django side — the same reason the writes are relayed.
 *
 * `Content-Disposition` comes from Django, which knows the filename; nothing is rewritten here.
 */
import { relay } from "../../../../../lib/django";

export const dynamic = "force-dynamic";

const INVOICE_ID = /^inv_[0-9a-f]{32}$/;

export async function GET(
  request: Request,
  { params }: { params: Promise<{ invoiceId: string }> },
): Promise<Response> {
  const { invoiceId } = await params;
  if (!INVOICE_ID.test(invoiceId)) return Response.json({ detail: "not found" }, { status: 404 });
  return relay(request, `/api/v1/app/invoices/${invoiceId}/pdf`, { method: "GET" });
}
