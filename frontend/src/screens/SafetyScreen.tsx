import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { type SafetyItem, api, errorMessage, unwrap } from "../api/client";
import { queryKeys } from "../api/queries";
import { Badge, Button, Dialog, Flash } from "../components/ui";
import { useKeyboard } from "../hooks/useKeyboard";
import { dateTime } from "../lib/format";
import { SafetyIncidents } from "./SafetyIncidents";
import { SafetyReleaseQueue } from "./SafetyReleaseQueue";

/**
 * Safety review is text only, always: no images, no media ids, and page URLs are plain text
 * (not links) because reviewers must not open flagged pages (docs/safety.md).
 */
export function SafetyScreen() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<number[]>([]);
  const [dialog, setDialog] = useState<"clear" | "block" | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const query = useQuery({
    queryKey: queryKeys.safety,
    queryFn: () => unwrap(api.GET("/api/v1/review/safety", { params: { query: { limit: 100 } } })),
  });
  const items = query.data?.items ?? [];

  useKeyboard((command) => {
    if (command.type === "escape") {
      if (dialog) setDialog(null);
      else setSelected([]);
    }
  });

  const toggle = (id: number) =>
    setSelected((current) => (current.includes(id) ? current.filter((value) => value !== id) : [...current, id]));

  const done = () => {
    setDialog(null);
    setSelected([]);
    void queryClient.invalidateQueries({ queryKey: queryKeys.safety });
    void queryClient.invalidateQueries({ queryKey: queryKeys.counts });
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-display text-display-m">Safety review</h1>
        <span className="text-body-s text-ink-muted">{items.length} pages{query.data?.next_cursor ? " (more pending)" : ""}</span>
        <div className="ml-auto flex gap-2">
          <Button onClick={() => setSelected(items.map((item) => item.id))} disabled={items.length === 0}>
            Select all
          </Button>
          <Button tone="confirm" onClick={() => setDialog("clear")} disabled={selected.length === 0}>
            Mark clear ({selected.length})
          </Button>
          <Button tone="reject" onClick={() => setDialog("block")} disabled={selected.length === 0}>
            Block… ({selected.length})
          </Button>
        </div>
      </div>

      <p className="rounded-sm border border-caution-ink bg-caution-quiet px-3 py-2 text-body-s text-caution-ink">
        Text only. Judge from the title, tags and matched terms. Do not open the page or its media. Anything
        suggesting a minor or real harm to a person: block it and follow the incident procedure in docs/safety.md.
        Unsure: leave it in review.
      </p>

      <Flash message={flash} onClose={() => setFlash(null)} />

      {query.isPending ? (
        <p className="text-ink-subtle">Loading…</p>
      ) : query.isError ? (
        <p className="text-critical-ink">Could not load: {errorMessage(query.error)}</p>
      ) : items.length === 0 ? (
        <div className="rounded-md border border-dashed border-hairline-strong bg-surface-100 p-10 text-center text-ink-muted">
          No pages waiting for safety review.
        </div>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((item) => (
            <SafetyRow key={item.id} item={item} checked={selected.includes(item.id)} onToggle={() => toggle(item.id)} />
          ))}
        </ul>
      )}

      {dialog === "clear" ? (
        <ClearDialog ids={selected} onClose={() => setDialog(null)} onDone={done} onError={setFlash} />
      ) : null}
      {dialog === "block" ? <BlockDialog ids={selected} onClose={() => setDialog(null)} onDone={done} /> : null}

      <SafetyReleaseQueue />

      <SafetyIncidents />
    </div>
  );
}

function SafetyRow({ item, checked, onToggle }: { item: SafetyItem; checked: boolean; onToggle: () => void }) {
  return (
    <li className={`rounded-md border p-3 ${checked ? "border-brand-ink bg-selection" : "border-hairline bg-surface-100"}`}>
      <label className="flex gap-3">
        <input type="checkbox" checked={checked} onChange={onToggle} className="mt-1" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{item.title || "(no title)"}</span>
            {item.categories.map((category) => (
              <Badge key={category} tone="caution">
                {category}
              </Badge>
            ))}
          </div>
          <div className="mt-1 text-caption text-ink-muted">
            {item.site} · flagged {dateTime(item.flagged_at)} · page {item.id}
          </div>
          <div className="mt-1 font-mono text-caption break-all text-ink-subtle">{item.url}</div>
          <div className="mt-2 flex flex-wrap gap-1 text-caption">
            {item.matches.map((match) => (
              <span key={`${match.term}-${match.category}`} className="rounded-sm bg-critical-quiet px-1.5 py-0.5 text-critical-ink">
                “{match.term}” · {match.category} · {match.action}
              </span>
            ))}
          </div>
          {item.tags.length ? <div className="mt-1 text-caption text-ink-muted">Tags: {item.tags.join(", ")}</div> : null}
        </div>
      </label>
    </li>
  );
}

function ClearDialog({
  ids,
  onClose,
  onDone,
  onError,
}: {
  ids: number[];
  onClose: () => void;
  onDone: () => void;
  onError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const submit = () => {
    setBusy(true);
    unwrap(api.POST("/api/v1/review/safety/clear", { body: { ids } })).then(onDone, (reason: unknown) => {
      onError(errorMessage(reason));
      onClose();
    });
  };
  return (
    <Dialog title={`Mark ${ids.length} page${ids.length === 1 ? "" : "s"} clear`} onClose={onClose}>
      <p className="text-body-s">
        Only where the flagged wording is clearly staged or fictional in a professional production. Their media is released to
        the fetch queue.
      </p>
      <div className="mt-5 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button tone="confirm" onClick={submit} disabled={busy}>
          Mark clear
        </Button>
      </div>
    </Dialog>
  );
}

function BlockDialog({ ids, onClose, onDone }: { ids: number[]; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const submit = () => {
    setBusy(true);
    unwrap(api.POST("/api/v1/review/safety/block", { body: { ids, reason: reason.trim() } })).then(
      onDone,
      (error: unknown) => {
        setBusy(false);
        setProblem(errorMessage(error));
      },
    );
  };
  return (
    <Dialog title={`Block ${ids.length} page${ids.length === 1 ? "" : "s"}`} onClose={onClose}>
      <label className="block text-body-s">
        <span className="mb-1 block text-ink-muted">Reason (required, stored as the safety note)</span>
        <textarea
          className="h-24 w-full rounded-sm border border-hairline-strong px-3 py-2"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          required
        />
      </label>
      <p className="mt-2 text-body-s text-ink-muted">
        Blocking cancels queued media and deletes frame hashes. Then follow the incident procedure (docs/safety.md).
      </p>
      {problem ? (
        <p role="alert" className="mt-3 text-body-s text-critical-ink">
          {problem}
        </p>
      ) : null}
      <div className="mt-5 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button tone="reject" onClick={submit} disabled={busy || reason.trim() === ""}>
          Block
        </Button>
      </div>
    </Dialog>
  );
}
