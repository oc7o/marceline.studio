import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { type Schemas, api, errorMessage, unwrap } from "../api/client";
import { queryKeys } from "../api/queries";
import { Badge, Button, Dialog, Flash } from "../components/ui";
import { dateTime } from "../lib/format";

export type ReleaseItem = Schemas["SafetyReleaseItemOut"];

/**
 * "Flagged term — customer credited" (docs/safety.md, operator decision 2026-09-17). These pages
 * are blocked. Text only, always: no images, no media ids, not even the page URL — the API sends
 * none. Staff release a page for fingerprinting (the server re-checks that no other safety term
 * matches and that a customer creator is still credited) or keep it blocked.
 */
export function SafetyReleaseQueue() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<number[]>([]);
  const [dialog, setDialog] = useState<"release" | "keep" | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const query = useQuery({
    queryKey: queryKeys.safetyRelease,
    queryFn: () =>
      unwrap(api.GET("/api/v1/review/safety/customer-release", { params: { query: { limit: 100 } } })),
  });
  const items = query.data?.items ?? [];

  const toggle = (id: number) =>
    setSelected((current) => (current.includes(id) ? current.filter((value) => value !== id) : [...current, id]));

  const done = () => {
    setDialog(null);
    setSelected([]);
    void queryClient.invalidateQueries({ queryKey: queryKeys.safetyRelease });
    void queryClient.invalidateQueries({ queryKey: queryKeys.counts });
  };

  return (
    <section className="flex flex-col gap-3" aria-labelledby="release-heading">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id="release-heading" className="font-display text-display-m">
          Flagged term — customer credited
        </h2>
        <span className="text-body-s text-ink-muted">
          {items.length} pages{query.data?.next_cursor ? " (more pending)" : ""}
        </span>
        <div className="ml-auto flex gap-2">
          <Button tone="confirm" onClick={() => setDialog("release")} disabled={selected.length === 0}>
            Release for fingerprinting… ({selected.length})
          </Button>
          <Button tone="reject" onClick={() => setDialog("keep")} disabled={selected.length === 0}>
            Keep blocked… ({selected.length})
          </Button>
        </div>
      </div>
      <p className="rounded-sm border border-caution-ink bg-caution-quiet px-3 py-2 text-body-s text-caution-ink">
        Blocked pages tagged &ldquo;flagged term&rdquo; that credit a customer&apos;s creator. Text only: judge from the
        title, tags, performer credits and the matched customer creator. Never look the page up. Release only a
        professional studio production in which nobody is or appears to be a minor; if there is any doubt, keep it blocked.
      </p>
      <Flash message={flash} onClose={() => setFlash(null)} />
      {query.isPending ? (
        <p className="text-ink-subtle">Loading…</p>
      ) : query.isError ? (
        <p className="text-critical-ink">Could not load: {errorMessage(query.error)}</p>
      ) : items.length === 0 ? (
        <div className="rounded-md border border-dashed border-hairline-strong bg-surface-100 p-6 text-center text-ink-muted">
          No blocked flagged-term pages credit a customer creator.
        </div>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((item) => (
            <ReleaseRow key={item.id} item={item} checked={selected.includes(item.id)} onToggle={() => toggle(item.id)} />
          ))}
        </ul>
      )}
      {dialog ? (
        <DecisionDialog
          kind={dialog}
          ids={selected}
          onClose={() => setDialog(null)}
          onDone={done}
          onError={(message) => {
            setFlash(message);
            setDialog(null);
          }}
        />
      ) : null}
    </section>
  );
}

export function ReleaseRow({ item, checked, onToggle }: { item: ReleaseItem; checked: boolean; onToggle: () => void }) {
  return (
    <li className={`rounded-md border p-3 ${checked ? "border-brand-ink bg-selection" : "border-hairline bg-surface-100"}`}>
      <label className="flex gap-3">
        <input type="checkbox" checked={checked} onChange={onToggle} className="mt-1" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{item.title || "(no title)"}</span>
            {item.customer_credits.map((credit) => (
              <Badge key={credit.creator_id} tone="accent">
                {credit.creator_name} · {credit.matched_in.join(", ")}
              </Badge>
            ))}
          </div>
          <div className="mt-1 text-caption text-ink-muted">
            {item.site} · flagged {dateTime(item.flagged_at)} · queued {dateTime(item.queued_at)} · page {item.id}
          </div>
          <div className="mt-2 flex flex-wrap gap-1 text-caption">
            {item.matches.map((match) => (
              <span key={`${match.term}-${match.action}`} className="rounded-sm bg-critical-quiet px-1.5 py-0.5 text-critical-ink">
                “{match.term}” · {match.category} · {match.action}
              </span>
            ))}
          </div>
          {item.performers.length ? (
            <div className="mt-1 text-caption text-ink-muted">Performers: {item.performers.join(", ")}</div>
          ) : null}
          {item.tags.length ? <div className="mt-1 text-caption text-ink-muted">Tags: {item.tags.join(", ")}</div> : null}
          {item.customer_credits.length === 0 ? (
            <div className="mt-1 text-caption text-critical-ink">No customer creator credited any more: keep it blocked.</div>
          ) : null}
        </div>
      </label>
    </li>
  );
}

function DecisionDialog({
  kind,
  ids,
  onClose,
  onDone,
  onError,
}: {
  kind: "release" | "keep";
  ids: number[];
  onClose: () => void;
  onDone: () => void;
  onError: (message: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const release = kind === "release";
  const plural = ids.length === 1 ? "" : "s";
  const submit = () => {
    setBusy(true);
    const body = { ids, reason: reason.trim() };
    const request = release
      ? api.POST("/api/v1/review/safety/customer-release/release", { body })
      : api.POST("/api/v1/review/safety/customer-release/keep-blocked", { body });
    unwrap(request).then(onDone, (error: unknown) => onError(errorMessage(error)));
  };
  return (
    <Dialog
      title={release ? `Release ${ids.length} page${plural} for fingerprinting` : `Keep ${ids.length} page${plural} blocked`}
      onClose={onClose}
    >
      <label className="block text-body-s">
        <span className="mb-1 block text-ink-muted">
          {release ? "Reason (required, stored with the release)" : "Reason (optional)"}
        </span>
        <textarea
          className="h-24 w-full rounded-sm border border-hairline-strong px-3 py-2"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          required={release}
        />
      </label>
      <p className="mt-2 text-body-s text-ink-muted">
        {release
          ? "The page becomes clear and its media may be fetched once it scores clearly safe. Any other safety term on a later crawl blocks it again."
          : "The page stays blocked; this decision stands on later crawls."}
      </p>
      <div className="mt-5 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button
          tone={release ? "confirm" : "reject"}
          onClick={submit}
          disabled={busy || (release && reason.trim() === "")}
        >
          {release ? "Release" : "Keep blocked"}
        </Button>
      </div>
    </Dialog>
  );
}
