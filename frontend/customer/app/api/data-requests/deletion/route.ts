/**
 * `POST /app/api/data-requests/deletion` -> ask for this account's data to be deleted (ADR 0056).
 *
 * The typed word travels in the body and is checked by Django — and again by development when the
 * write arrives through the outbox. Nothing is decided here: this relays, like every handler.
 */
import { relay } from "../../../../lib/django";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return relay(request, "/api/v1/app/data-requests/deletion", { method: "POST" });
}
