import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { type Schemas, api, errorMessage, unwrap } from "../api/client";
import { queryKeys } from "../api/queries";
import { Badge, Button, Dialog, Flash } from "../components/ui";
import { resolveTargetId } from "../lib/creatorReview";
import { dateTime } from "../lib/format";

type CreatorItem = Schemas["CreatorReviewItemOut"];
type SimilarCreator = Schemas["SimilarCreatorOut"];

/**
 * Staff review queue for `catalog.Creator.needs_review` rows (issue #29 follow-up, operator
 * 2026-09-17): names a customer typed that matched no existing creator by exact name. Nothing
 * here is automatic — every merge or alias needs a target and a confirmation click.
 */
export function CreatorsScreen() {
  const [flash, setFlash] = useState<{ message: string; tone: "error" | "info" } | null>(null);
  const [dialog, setDialog] = useState<{ creator: CreatorItem; mode: "merge" | "alias" } | null>(null);
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.creatorReview,
    queryFn: () => unwrap(api.GET("/api/v1/review/creators")),
  });
  const items = query.data?.items ?? [];

  const refresh = (message: string) => {
    setDialog(null);
    setFlash({ message, tone: "info" });
    void queryClient.invalidateQueries({ queryKey: queryKeys.creatorReview });
    void queryClient.invalidateQueries({ queryKey: queryKeys.counts });
  };

  const markReviewed = (creator: CreatorItem) => {
    unwrap(
      api.POST("/api/v1/review/creators/{creator_id}/mark-reviewed", {
        params: { path: { creator_id: creator.id } },
      }),
    ).then(
      () => refresh(`${creator.name} marked reviewed.`),
      (error: unknown) => setFlash({ message: errorMessage(error), tone: "error" }),
    );
  };

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-display text-display-m">Creators to review</h1>
        <span className="text-body-s text-ink-muted">
          {items.length} waiting{query.data?.truncated ? " (more not shown)" : ""}
        </span>
      </div>
      <p className="rounded-sm border border-caution-ink bg-caution-quiet px-3 py-2 text-body-s text-caution-ink">
        A name a customer typed that matched no existing creator by exact name. It is not crawled, linked to pages or
        used for discovery until you decide: mark it reviewed as a genuinely new creator, merge it into an existing
        one (a duplicate identity), or record it as an alias of an existing creator (a studio/brand name).
      </p>
      <Flash message={flash?.message ?? null} tone={flash?.tone} onClose={() => setFlash(null)} />
      {query.isPending ? (
        <p className="text-ink-subtle">Loading…</p>
      ) : query.isError ? (
        <p className="text-critical-ink">Could not load: {errorMessage(query.error)}</p>
      ) : items.length === 0 ? (
        <div className="rounded-md border border-dashed border-border-control bg-surface-100 p-6 text-center text-ink-muted">
          Nothing waiting on review.
        </div>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((creator) => (
            <li key={creator.id} className="rounded-sm border border-hairline bg-surface-100 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{creator.name}</span>
                <span className="text-caption text-ink-muted">created {dateTime(creator.created_at)}</span>
              </div>
              <div className="mt-1 text-caption text-ink-muted">
                {creator.organisations.length > 0 ? `Uploaded by ${creator.organisations.join(", ")}` : "No customer upload on file"} ·{" "}
                {creator.linked_references} linked original{creator.linked_references === 1 ? "" : "s"}
              </div>
              {creator.similar.length > 0 ? (
                <div className="mt-2 flex flex-wrap items-center gap-1 text-caption">
                  <span className="text-ink-muted">Possible existing match:</span>
                  {creator.similar.map((match) => (
                    <Badge key={match.id} tone="caution">
                      {match.name} ({Math.round(match.similarity * 100)}%)
                    </Badge>
                  ))}
                </div>
              ) : null}
              <div className="mt-2 flex flex-wrap gap-2">
                <Button tone="confirm" onClick={() => markReviewed(creator)}>
                  Mark reviewed
                </Button>
                <Button onClick={() => setDialog({ creator, mode: "merge" })}>Merge into…</Button>
                <Button onClick={() => setDialog({ creator, mode: "alias" })}>Add as alias of…</Button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {dialog ? (
        <MergeDialog
          creator={dialog.creator}
          mode={dialog.mode}
          onClose={() => setDialog(null)}
          onDone={(message) => refresh(message)}
          onError={(message) => setFlash({ message, tone: "error" })}
        />
      ) : null}
    </section>
  );
}

function MergeDialog({
  creator,
  mode,
  onClose,
  onDone,
  onError,
}: {
  creator: CreatorItem;
  mode: "merge" | "alias";
  onClose: () => void;
  onDone: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [targetId, setTargetId] = useState<number | null>(creator.similar[0]?.id ?? null);
  const [manualId, setManualId] = useState("");
  const [busy, setBusy] = useState(false);
  const path =
    mode === "merge" ? "/api/v1/review/creators/{creator_id}/merge" : "/api/v1/review/creators/{creator_id}/alias";
  const verb = mode === "merge" ? "Merge" : "Add as alias";

  const submit = () => {
    const resolved = resolveTargetId(targetId, manualId);
    if (resolved == null) {
      onError("Choose a target creator or enter a valid creator id.");
      return;
    }
    setBusy(true);
    unwrap(
      api.POST(path, {
        params: { path: { creator_id: creator.id } },
        body: { target_id: resolved },
      }),
    ).then(
      (result) => onDone(`${result.source_name} ${mode === "merge" ? "merged into" : "recorded as an alias of"} ${result.target_name}.`),
      (error: unknown) => {
        setBusy(false);
        onError(errorMessage(error));
      },
    );
  };

  return (
    <Dialog title={`${verb} “${creator.name}”`} onClose={onClose}>
      <div className="flex flex-col gap-3 text-body-s">
        <p className="text-ink-muted">
          {mode === "merge"
            ? "Every reference cast link and page link moves to the target creator; the source's aliases join the target's, and the source is deleted."
            : "The same move as a merge, and the typed name itself joins the target's aliases — for a studio or brand name, not a person."}
        </p>
        {creator.similar.length > 0 ? (
          <fieldset className="flex flex-col gap-1">
            <legend className="mb-1 text-ink-muted">Possible match</legend>
            {creator.similar.map((match: SimilarCreator) => (
              <label key={match.id} className="flex items-center gap-2">
                <input
                  type="radio"
                  name="target"
                  checked={targetId === match.id}
                  onChange={() => {
                    setTargetId(match.id);
                    setManualId("");
                  }}
                />
                <span>
                  {match.name} ({Math.round(match.similarity * 100)}% similar)
                  {match.aliases.length > 0 ? ` · aliases: ${match.aliases.join(", ")}` : ""}
                </span>
              </label>
            ))}
          </fieldset>
        ) : null}
        <label className="block">
          <span className="mb-1 block text-ink-muted">Or another creator id (see the admin)</span>
          <input
            className="w-full rounded-sm border border-hairline-strong px-3 py-1.5"
            inputMode="numeric"
            value={manualId}
            onChange={(event) => {
              setManualId(event.target.value);
              setTargetId(null);
            }}
            placeholder="e.g. 42"
          />
        </label>
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button tone="primary" onClick={submit} disabled={busy}>
          {verb}
        </Button>
      </div>
    </Dialog>
  );
}
