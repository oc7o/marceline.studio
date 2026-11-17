import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { type Schemas, api, errorMessage, unwrap } from "../api/client";
import { queryKeys } from "../api/queries";
import { Badge, Button, Dialog, Flash } from "../components/ui";
import { claimReasonLabel } from "../lib/claimReasons";
import { dateTime } from "../lib/format";

type Claim = Schemas["ClaimItemOut"];
type ClaimStatus = "open" | "resolved" | "dismissed" | "all";

/**
 * Staff queue for refused customer upload claims (issue #29 amendment, operator 2026-09-17). Every
 * refused claim answers the customer with one neutral 409 that names nobody (ADR 0019: deciding
 * rights ownership is not something a server can do); the reason, the organisation and the
 * original(s) touched are staff-only, from here.
 */
export function ClaimsScreen() {
  const [status, setStatus] = useState<ClaimStatus>("open");
  const [flash, setFlash] = useState<{ message: string; tone: "error" | "info" } | null>(null);
  const [deciding, setDeciding] = useState<{ claim: Claim; action: "resolve" | "dismiss" } | null>(null);
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.claims(status),
    queryFn: () => unwrap(api.GET("/api/v1/review/claims", { params: { query: { status } } })),
  });
  const items = query.data?.items ?? [];

  const refresh = (message: string) => {
    setDeciding(null);
    setFlash({ message, tone: "info" });
    void queryClient.invalidateQueries({ queryKey: ["claims"] });
    void queryClient.invalidateQueries({ queryKey: queryKeys.counts });
  };

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-display text-display-m">Refused upload claims</h1>
        <label className="text-body-s">
          <span className="sr-only">Status</span>
          <select
            className="rounded-sm border border-border-control bg-surface-100 px-2 py-1.5"
            value={status}
            onChange={(event) => setStatus(event.target.value as ClaimStatus)}
          >
            <option value="open">Open</option>
            <option value="resolved">Resolved</option>
            <option value="dismissed">Dismissed</option>
            <option value="all">All</option>
          </select>
        </label>
        <span className="text-body-s text-ink-muted">
          {items.length} shown{query.data?.truncated ? " (more not shown)" : ""}
        </span>
      </div>
      <p className="rounded-sm border border-caution-ink bg-caution-quiet px-3 py-2 text-body-s text-caution-ink">
        The customer only ever saw one generic answer. Never tell them which case applied or about whom — deciding
        rights ownership is a human, not a server (ADR 0019).
      </p>
      <Flash message={flash?.message ?? null} tone={flash?.tone} onClose={() => setFlash(null)} />
      {query.isPending ? (
        <p className="text-ink-subtle">Loading…</p>
      ) : query.isError ? (
        <p className="text-critical-ink">Could not load: {errorMessage(query.error)}</p>
      ) : items.length === 0 ? (
        <div className="rounded-md border border-dashed border-border-control bg-surface-100 p-6 text-center text-ink-muted">
          Nothing here.
        </div>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((claim) => (
            <li key={claim.id} className="rounded-sm border border-hairline bg-surface-100 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{claim.organisation_name}</span>
                <Badge tone="caution">{claimReasonLabel(claim.reason)}</Badge>
                <Badge>{claim.status}</Badge>
              </div>
              <div className="mt-1 font-mono text-caption break-all text-ink-muted">{claim.sha256}</div>
              <div className="mt-1 text-caption text-ink-muted">
                {claim.overlap_share != null ? `${Math.round(claim.overlap_share * 100)}% hash overlap · ` : ""}
                requested {dateTime(claim.created_at)}
                {claim.requested_by ? ` by ${claim.requested_by}` : ""}
              </div>
              {claim.references.length > 0 ? (
                <ul className="mt-1 text-caption text-ink-muted">
                  {claim.references.map((reference) => (
                    <li key={reference.id}>
                      Original #{reference.id}: “{reference.title}” — {reference.creator_name}
                    </li>
                  ))}
                </ul>
              ) : null}
              {claim.note ? <p className="mt-1 text-caption text-ink-muted">Note: {claim.note}</p> : null}
              {claim.resolved_by ? (
                <p className="mt-1 text-caption text-ink-muted">
                  {claim.status} {dateTime(claim.resolved_at)} by {claim.resolved_by}
                </p>
              ) : null}
              {claim.status === "open" ? (
                <div className="mt-2 flex flex-wrap gap-2">
                  <Button tone="confirm" onClick={() => setDeciding({ claim, action: "resolve" })}>
                    Resolve…
                  </Button>
                  <Button tone="reject" onClick={() => setDeciding({ claim, action: "dismiss" })}>
                    Dismiss…
                  </Button>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {deciding ? (
        <DecisionDialog
          claim={deciding.claim}
          action={deciding.action}
          onClose={() => setDeciding(null)}
          onDone={refresh}
          onError={(message) => setFlash({ message, tone: "error" })}
        />
      ) : null}
    </section>
  );
}

function DecisionDialog({
  claim,
  action,
  onClose,
  onDone,
  onError,
}: {
  claim: Claim;
  action: "resolve" | "dismiss";
  onClose: () => void;
  onDone: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const path = action === "resolve" ? "/api/v1/review/claims/{claim_id}/resolve" : "/api/v1/review/claims/{claim_id}/dismiss";

  const submit = () => {
    setBusy(true);
    unwrap(
      api.POST(path, {
        params: { path: { claim_id: claim.id } },
        body: { note },
      }),
    ).then(
      () => onDone(`Claim ${action === "resolve" ? "resolved" : "dismissed"}.`),
      (error: unknown) => {
        setBusy(false);
        onError(errorMessage(error));
      },
    );
  };

  return (
    <Dialog title={`${action === "resolve" ? "Resolve" : "Dismiss"} claim`} onClose={onClose}>
      <label className="block text-body-s">
        <span className="mb-1 block text-ink-muted">Note (optional, staff-only)</span>
        <textarea
          className="w-full rounded-sm border border-hairline-strong px-3 py-1.5"
          rows={4}
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
      </label>
      <div className="mt-5 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button tone="primary" onClick={submit} disabled={busy}>
          {action === "resolve" ? "Resolve" : "Dismiss"}
        </Button>
      </div>
    </Dialog>
  );
}
