import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useMemo, useReducer, useRef, useState } from "react";

import {
  type ClusterItem,
  type ClusterMember,
  type LinkedOriginal,
  api,
  errorMessage,
  unwrap,
} from "../api/client";
import { queryKeys, useReferenceSummaries } from "../api/queries";
import { AvailabilityBadge } from "../components/Availability";
import { MediaGrid } from "../components/MediaGrid";
import { StatusLegend } from "../components/StatusLegend";
import { Badge, Button, Dialog, ExternalLink, Flash, ShortcutsDialog, StatusBadge } from "../components/ui";
import { useKeyboard } from "../hooks/useKeyboard";
import { isRemoved, recheckMessage } from "../lib/availability";
import { clock, creatorLabel, dateTime } from "../lib/format";
import { useImageMode } from "../lib/imageMode";
import { evidenceLabel, linkEvidence, linkedOriginalSummary, provenanceLabel } from "../lib/provenance";
import {
  type FinalDecision,
  type ItemStatus,
  type ReviewAction,
  type ReviewState,
  initialReviewState,
  reviewReducer,
  statusOf,
  undoable,
} from "../lib/reviewReducer";
import type { ClusterShow } from "../router";

interface MemberUndo {
  memberIds: number[];
}

const reducer = (state: ReviewState<MemberUndo>, action: ReviewAction<MemberUndo>) =>
  reviewReducer(state, action);

const memberKey = (member: ClusterMember) => `m${member.id}`;
const asItem = (member: ClusterMember) => ({ key: memberKey(member), status: member.status as ItemStatus });

function sendMembers(ids: number[], decision: FinalDecision | "reset") {
  return unwrap(api.POST("/api/v1/review/clusters/members/decide", { body: { ids, decision } }));
}

export function ClustersScreen() {
  const search = useSearch({ from: "/clusters" });
  const show: ClusterShow = search.show ?? "all";
  const [generation, setGeneration] = useState(0);
  // Remount per filter: position, selection and undo history belong to one queue.
  return (
    <ClusterQueue
      key={`${generation}-${show}-${search.reference ?? "any"}`}
      show={show}
      reference={search.reference}
      hideRemoved={search.hideRemoved ?? false}
      onRefresh={() => setGeneration((value) => value + 1)}
    />
  );
}

function ClusterQueue({
  show,
  reference,
  hideRemoved,
  onRefresh,
}: {
  show: ClusterShow;
  reference: number | undefined;
  hideRemoved: boolean;
  onRefresh: () => void;
}) {
  const navigate = useNavigate({ from: "/clusters" });
  const queryClient = useQueryClient();
  const summaries = useReferenceSummaries();
  const [mode] = useImageMode();
  // Here `index` is the cluster position; overrides and selection are keyed by member.
  const [state, dispatch] = useReducer(reducer, undefined, () => initialReviewState<MemberUndo>());
  const [dialog, setDialog] = useState<"help" | "merge" | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [rechecking, setRechecking] = useState(false);
  const recordIds = useRef(0);

  const { data, hasNextPage, isFetchingNextPage, fetchNextPage, isPending, isError, error } = useInfiniteQuery({
    queryKey: [...queryKeys.clusters, show, reference ?? null],
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/v1/review/clusters", {
          params: { query: { show, reference_id: reference, cursor: pageParam, limit: 10 } },
        }),
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    staleTime: Infinity,
    gcTime: 0,
  });
  const clusters = useMemo(() => data?.pages.flatMap((page) => page.items) ?? [], [data]);
  const total = clusters.length;
  const cluster: ClusterItem | undefined = clusters[state.index];
  const visible = cluster
    ? cluster.members.filter((member) => !(hideRemoved && isRemoved(member.page)))
    : [];

  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && state.index >= total - 2) void fetchNextPage();
  }, [hasNextPage, isFetchingNextPage, state.index, total, fetchNextPage]);

  // Hidden (removed) members are never decided in bulk.
  const targets = (): ClusterMember[] => {
    const selected = visible.filter((member) => state.selected.includes(memberKey(member)));
    if (selected.length) return selected;
    return visible.filter((member) => statusOf(state, asItem(member)) === "proposed");
  };

  const decide = (decision: FinalDecision) => {
    const members = targets();
    if (members.length === 0) return;
    const id = ++recordIds.current;
    const previous = Object.fromEntries(members.map((member) => [memberKey(member), statusOf(state, asItem(member))]));
    dispatch({ type: "decided", id, keys: members.map(memberKey), decision, previous, advance: false, total });
    sendMembers(
      members.map((member) => member.id),
      decision,
    ).then(
      (result) => {
        dispatch({ type: "settled", id, undo: { memberIds: result.member_ids } });
        void queryClient.invalidateQueries({ queryKey: queryKeys.counts });
      },
      (reason: unknown) => {
        dispatch({ type: "failed", id });
        setFlash(errorMessage(reason));
      },
    );
  };

  const undo = () => {
    const record = undoable(state);
    if (!record?.undo) return;
    dispatch({ type: "undone" });
    if (record.undo.memberIds.length === 0) return;
    sendMembers(record.undo.memberIds, "reset").then(
      () => void queryClient.invalidateQueries({ queryKey: queryKeys.counts }),
      (reason: unknown) => setFlash(`Undo failed: ${errorMessage(reason)}`),
    );
  };

  const resetMember = (member: ClusterMember) => {
    sendMembers([member.id], "reset").then(
      (result) => {
        dispatch({ type: "setStatus", keys: [memberKey(member)], status: "proposed" });
        void queryClient.invalidateQueries({ queryKey: queryKeys.counts });
        const notes = result.notes ?? [];
        setNotice(notes.length ? notes.join(" ") : `Member ${member.id} is proposed again.`);
      },
      (reason: unknown) => setFlash(errorMessage(reason)),
    );
  };

  const recheck = (target: ClusterItem) => {
    setRechecking(true);
    unwrap(api.POST("/api/v1/review/clusters/{cluster_id}/recheck", { params: { path: { cluster_id: target.id } } }))
      .then(
        (result) => setNotice(recheckMessage(result)),
        (reason: unknown) => setFlash(errorMessage(reason)),
      )
      .finally(() => setRechecking(false));
  };

  useKeyboard((command) => {
    if (dialog) {
      if (command.type === "escape" || (dialog === "help" && command.type === "help")) setDialog(null);
      return;
    }
    switch (command.type) {
      case "confirm":
        return decide("confirm");
      case "reject":
        return decide("reject");
      case "skip":
        dispatch({ type: "clearSelection" });
        return dispatch({ type: "next", total });
      case "back":
        dispatch({ type: "clearSelection" });
        return dispatch({ type: "prev" });
      case "undo":
        return undo();
      case "help":
        return setDialog("help");
      case "escape":
        setFlash(null);
        setNotice(null);
        return dispatch({ type: "clearSelection" });
      default:
        return;
    }
  });

  const pending = cluster ? targets() : [];
  const selectionActive = state.selected.length > 0;
  const hiddenCount = cluster ? cluster.members.length - visible.length : 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-display text-display-m">Cluster proposals</h1>
        <label className="text-body-s">
          <span className="sr-only">Which clusters</span>
          <select
            className="rounded-sm border border-border-control bg-surface-100 px-2 py-1.5"
            value={show}
            onChange={(event) =>
              void navigate({
                search: (prev) => ({
                  ...prev,
                  show: event.target.value === "linked" ? "linked" : undefined,
                }),
              })
            }
          >
            <option value="all">All clusters</option>
            <option value="linked">Linked to an original only</option>
          </select>
        </label>
        <label className="text-body-s">
          <span className="sr-only">Original</span>
          <select
            className="max-w-xs rounded-sm border border-border-control bg-surface-100 px-2 py-1.5"
            value={reference ?? ""}
            onChange={(event) =>
              void navigate({
                search: (prev) => ({ ...prev, reference: event.target.value ? Number(event.target.value) : undefined }),
              })
            }
          >
            <option value="">Any original</option>
            {(summaries.data ?? []).map((summary) => (
              <option key={summary.id} value={summary.id}>
                {creatorLabel(summary.creator_names)} — {summary.title}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-body-s">
          <input
            type="checkbox"
            checked={hideRemoved}
            onChange={(event) =>
              void navigate({ search: (prev) => ({ ...prev, hideRemoved: event.target.checked ? true : undefined }) })
            }
          />
          Hide removed pages
        </label>
        <StatusLegend />
        <span className="text-body-s text-ink-muted" aria-live="polite">
          Cluster {Math.min(state.index + 1, total)} / {total}
          {hasNextPage ? "+" : ""}
        </span>
        <div className="ml-auto flex gap-2">
          <Button onClick={() => dispatch({ type: "prev" })} shortcut="←">
            Previous
          </Button>
          <Button onClick={() => dispatch({ type: "next", total })} shortcut="→">
            Next
          </Button>
          <Button onClick={undo} disabled={!undoable(state)} shortcut="U">
            Undo
          </Button>
          <Button onClick={onRefresh}>Refresh queue</Button>
          <Button onClick={() => setDialog("help")} shortcut="?">
            Shortcuts
          </Button>
        </div>
      </div>

      <Flash message={flash} onClose={() => setFlash(null)} />
      <Flash message={notice} onClose={() => setNotice(null)} tone="info" />

      {isPending ? (
        <p className="text-ink-subtle">Loading clusters…</p>
      ) : isError ? (
        <p className="text-critical-ink">Could not load clusters: {errorMessage(error)}</p>
      ) : !cluster ? (
        <div className="rounded-md border border-dashed border-hairline-strong bg-surface-100 p-10 text-center text-ink-muted">
          {total > 0
            ? "End of the queue. Press ← to go back."
            : show === "linked" || reference
              ? "No open cluster is linked to this filter's originals. Choose “All clusters” to see the rest."
              : "No cluster proposals."}
        </div>
      ) : (
        <section aria-label={`Cluster ${cluster.id}`} className="rounded-md border border-hairline bg-surface-100 p-5 shadow-sm">
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <h2 className="text-xl font-semibold">Cluster #{cluster.id}</h2>
            <Badge>{cluster.member_count} active</Badge>
            <Badge tone="accent">{cluster.proposed_count} proposed</Badge>
            {cluster.borderline_count ? <Badge tone="caution">{cluster.borderline_count} borderline</Badge> : null}
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <Button onClick={() => recheck(cluster)} disabled={rechecking}>
                Check availability now
              </Button>
              <span className="text-body-s text-ink-muted">
                {selectionActive ? `${state.selected.length} selected` : `all ${pending.length} proposed`}
              </span>
              <Button tone="confirm" onClick={() => decide("confirm")} disabled={pending.length === 0} shortcut="Y">
                Confirm
              </Button>
              <Button tone="reject" onClick={() => decide("reject")} disabled={pending.length === 0} shortcut="N">
                Reject
              </Button>
              <Button onClick={() => setDialog("merge")}>Merge…</Button>
            </div>
          </div>
          <LinkedOriginals originals={cluster.linked_originals} />
          <MemberTable
            members={visible}
            state={state}
            dispatch={dispatch}
            mode={mode}
            onReset={resetMember}
          />
          {hiddenCount ? (
            <p className="mt-2 text-caption text-ink-subtle">
              {hiddenCount} removed page{hiddenCount === 1 ? "" : "s"} hidden (not included in Confirm / Reject).
            </p>
          ) : null}
        </section>
      )}

      {dialog === "help" ? (
        <ShortcutsDialog
          onClose={() => setDialog(null)}
          extra="On clusters, Y / N apply to the checked members, or to all visible proposed members of the cluster when nothing is checked. S / → and ← move between clusters."
        />
      ) : null}
      {dialog === "merge" && cluster ? (
        <MergeDialog
          cluster={cluster}
          onClose={() => setDialog(null)}
          onMerged={() => {
            setDialog(null);
            onRefresh();
          }}
        />
      ) : null}
    </div>
  );
}

function LinkedOriginals({ originals }: { originals: LinkedOriginal[] }) {
  if (originals.length === 0) {
    return (
      <p className="mb-3 rounded-sm border border-dashed border-hairline-strong bg-surface-inset px-3 py-2 text-body-s text-ink-muted">
        <strong>Not linked to any original.</strong> These pages look like copies of one video, but none of them has a
        fingerprint match or a leak entry for your originals.
      </p>
    );
  }
  return (
    <div className="mb-3 rounded-sm border border-accent bg-accent-quiet px-3 py-2 text-body-s text-accent">
      <h3 className="font-semibold">Linked originals</h3>
      <ul className="mt-1 list-disc space-y-0.5 pl-5">
        {originals.map((original) => (
          <li key={original.reference.id}>{linkedOriginalSummary(original)}</li>
        ))}
      </ul>
    </div>
  );
}

function MemberTable({
  members,
  state,
  dispatch,
  mode,
  onReset,
}: {
  members: ClusterMember[];
  state: ReviewState<MemberUndo>;
  dispatch: React.Dispatch<ReviewAction<MemberUndo>>;
  mode: ReturnType<typeof useImageMode>[0];
  onReset: (member: ClusterMember) => void;
}) {
  const order = members.map(memberKey);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-body-s">
        <thead className="bg-surface-200 text-overline uppercase text-ink-muted">
          <tr>
            <th className="w-10 px-2 py-1.5">
              <span className="sr-only">Select</span>
            </th>
            <th className="px-2 py-1.5">Status</th>
            <th className="px-2 py-1.5">Page</th>
            <th className="px-2 py-1.5">Duration</th>
            <th className="px-2 py-1.5">Evidence</th>
            <th className="px-2 py-1.5">Min dist.</th>
            <th className="px-2 py-1.5">Uploaded</th>
            <th className="px-2 py-1.5">Image</th>
          </tr>
        </thead>
        <tbody>
          {members.map((member) => {
            const key = memberKey(member);
            const status = statusOf(state, asItem(member));
            const removed = isRemoved(member.page);
            const dimmed = removed ? "opacity-50" : status === "rejected" ? "opacity-60" : "";
            return (
              <tr key={key} className={`border-t border-hairline align-top ${dimmed}`}>
                <td className="px-2 py-2">
                  <input
                    type="checkbox"
                    aria-label={`Select member ${member.id}`}
                    checked={state.selected.includes(key)}
                    onClick={(event) =>
                      dispatch(event.shiftKey ? { type: "selectRange", order, key } : { type: "toggleSelect", key })
                    }
                    onChange={() => undefined}
                  />
                </td>
                <td className="w-56 px-2 py-2">
                  <StatusBadge status={status} />
                  <div className="mt-1 text-caption text-ink-muted" title="Derived from the evidence field and your pair labels">
                    {provenanceLabel({
                      status,
                      serverStatus: member.status,
                      provenance: member.provenance,
                      evidence: member.evidence,
                    })}
                  </div>
                  {status !== "proposed" ? (
                    <button
                      type="button"
                      className="mt-1 text-caption text-brand-ink underline"
                      onClick={() => onReset(member)}
                      aria-label={`Reset member ${member.id} to proposed`}
                    >
                      Reset to proposed
                    </button>
                  ) : null}
                </td>
                <td className="max-w-md px-2 py-2">
                  <div className={`font-medium ${removed ? "line-through" : ""}`}>{member.page.title || "(no title)"}</div>
                  <div className="flex flex-wrap items-center gap-2 text-caption text-ink-muted">
                    <span>
                      page {member.page.id} · {member.page.site}
                    </span>
                    <AvailabilityBadge page={member.page} />
                  </div>
                  <div className="text-caption">
                    <ExternalLink url={member.page.url} />
                  </div>
                  {member.originals.length ? (
                    <ul className="mt-1 flex flex-wrap gap-1" aria-label="Links to originals">
                      {member.originals.map((link) => (
                        <li key={link.reference_id}>
                          <Badge tone={link.status === "rejected" ? "neutral" : link.match_strength === "strong" ? "accent" : "caution"}>
                            {linkEvidence(link)} · {link.reference_title}
                          </Badge>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </td>
                <td className="px-2 py-2 tabular-nums">
                  {clock(member.page.duration_s)}
                  {member.possible_clip ? (
                    <div>
                      <Badge tone="caution">possible clip</Badge>
                    </div>
                  ) : null}
                </td>
                <td className="px-2 py-2">
                  {evidenceLabel(member.evidence)}
                  {member.borderline ? (
                    <div>
                      <Badge tone="caution">borderline</Badge>
                    </div>
                  ) : null}
                </td>
                <td className="px-2 py-2 tabular-nums">{member.min_distance ?? "—"}</td>
                <td className="px-2 py-2 whitespace-nowrap">{dateTime(member.page.uploaded_at)}</td>
                <td className="px-2 py-2">
                  <MediaGrid
                    media={member.media.slice(0, 1)}
                    hidden={member.page.safety_status !== "clear"}
                    mode={mode}
                    size="sm"
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function MergeDialog({ cluster, onClose, onMerged }: { cluster: ClusterItem; onClose: () => void; onMerged: () => void }) {
  const [others, setOthers] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const ids = others
    .split(/[\s,]+/)
    .map((value) => Number(value.replace(/^#/, "")))
    .filter((value) => Number.isInteger(value) && value > 0 && value !== cluster.id);
  const submit = () => {
    setBusy(true);
    unwrap(api.POST("/api/v1/review/clusters/merge", { body: { target_id: cluster.id, source_ids: ids } })).then(
      onMerged,
      (reason: unknown) => {
        setBusy(false);
        setProblem(errorMessage(reason));
      },
    );
  };
  return (
    <Dialog title={`Merge into cluster #${cluster.id}`} onClose={onClose}>
      <label className="block text-body-s">
        <span className="mb-1 block text-ink-muted">Other cluster ids (comma or space separated)</span>
        <input
          className="w-full rounded-sm border border-hairline-strong px-3 py-2"
          value={others}
          onChange={(event) => setOthers(event.target.value)}
          placeholder="e.g. 12, 15"
        />
      </label>
      <p className="mt-2 text-body-s text-ink-muted">
        Members keep their decisions; conflicting human decisions abort the merge.
      </p>
      {problem ? (
        <p role="alert" className="mt-3 text-body-s text-critical-ink">
          {problem}
        </p>
      ) : null}
      <div className="mt-5 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button tone="primary" onClick={submit} disabled={busy || ids.length === 0}>
          Merge {ids.length} cluster{ids.length === 1 ? "" : "s"}
        </Button>
      </div>
    </Dialog>
  );
}
