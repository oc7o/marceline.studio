import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useMemo, useReducer, useRef, useState } from "react";

import {
  type Decision,
  type ReferenceItem,
  type ReferenceSummary,
  type Schemas,
  api,
  errorMessage,
  unwrap,
} from "../api/client";
import { queryKeys, useReferenceSummaries } from "../api/queries";
import { AvailabilityBadge } from "../components/Availability";
import { MediaGrid } from "../components/MediaGrid";
import { Badge, Button, Dialog, ExternalLink, Field, Flash, ShortcutsDialog, StatusBadge } from "../components/ui";
import { useKeyboard } from "../hooks/useKeyboard";
import { isRemoved, recheckMessage } from "../lib/availability";
import {
  BANDS,
  type Band,
  DECIDED_BY,
  type DecidedByFilter,
  bandClasses,
  bandFilterText,
  bandLabel,
  cycleBands,
  evidenceNotes,
  parseDecidedBy,
  signalBreakdown,
  signalTotal,
  thresholdsText,
  timelineText,
  toggleBand,
} from "../lib/confidence";
import { copyExtentLabel, copyExtentText, copyExtentTone } from "../lib/copyExtent";
import { clock, creatorLabel, dateTime, openExternal, percent, signedSeconds } from "../lib/format";
import { type ImageMode, useImageMode } from "../lib/imageMode";
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
import { type ReferenceSearch, STATUS_FILTERS, type StatusFilter } from "../router";

const PAGE_SIZE = 25;
const PREFETCH_MARGIN = 5;

type BandThresholds = Schemas["BandThresholdsOut"];
type ReferenceMatch = NonNullable<ReferenceItem["match"]>;

/** Changed rows of a pair decision: resetting exactly these undoes it. */
export interface PairUndo {
  leakIds: number[];
  matchIds: number[];
}

const reducer = (state: ReviewState<PairUndo>, action: ReviewAction<PairUndo>) =>
  reviewReducer(state, action);

async function sendPairDecision(targets: ReferenceItem[], decision: Decision): Promise<PairUndo> {
  // A pair decision cascades server-side to the other row of the same (reference, page).
  const leakIds = targets.flatMap((item) => (item.leak ? [item.leak.id] : []));
  const matchIds = targets.flatMap((item) => (!item.leak && item.match ? [item.match.id] : []));
  const leaks = leakIds.length
    ? await unwrap(api.POST("/api/v1/review/references/leaks/decide", { body: { ids: leakIds, decision } }))
    : null;
  const matches = matchIds.length
    ? await unwrap(api.POST("/api/v1/review/references/matches/decide", { body: { ids: matchIds, decision } }))
    : null;
  return {
    leakIds: [...(leaks?.leak_ids ?? []), ...(matches?.leak_ids ?? [])],
    matchIds: [...(leaks?.match_ids ?? []), ...(matches?.match_ids ?? [])],
  };
}

async function sendReset(undo: PairUndo): Promise<void> {
  if (undo.leakIds.length) {
    await unwrap(api.POST("/api/v1/review/references/leaks/decide", { body: { ids: undo.leakIds, decision: "reset" } }));
  }
  if (undo.matchIds.length) {
    await unwrap(api.POST("/api/v1/review/references/matches/decide", { body: { ids: undo.matchIds, decision: "reset" } }));
  }
}

export function ReferencesScreen() {
  const search = useSearch({ from: "/references" });
  const status = search.status ?? "proposed";
  const filterKey = [
    search.reference ?? "all",
    status,
    search.site ?? "all",
    search.hideRemoved ? "hide" : "show",
    search.band?.join("+") ?? "bands",
    search.includeLow ? "low" : "nolow",
    search.decidedBy ?? "anyone",
  ].join("-");
  // Remount per filter: position, selection and undo history belong to one queue.
  return (
    <ReferenceQueue
      key={filterKey}
      reference={search.reference}
      status={status}
      site={search.site}
      hideRemoved={search.hideRemoved ?? false}
      bands={search.band}
      includeLow={search.includeLow ?? false}
      decidedBy={search.decidedBy}
    />
  );
}

function ReferenceQueue({
  reference,
  status,
  site,
  hideRemoved,
  bands,
  includeLow,
  decidedBy,
}: {
  reference: number | undefined;
  status: StatusFilter;
  site: number | undefined;
  hideRemoved: boolean;
  bands: Band[] | undefined;
  includeLow: boolean;
  decidedBy: DecidedByFilter | undefined;
}) {
  const navigate = useNavigate({ from: "/references" });
  const queryClient = useQueryClient();
  const [mode] = useImageMode();
  const summaries = useReferenceSummaries();
  const [state, dispatch] = useReducer(reducer, undefined, () => initialReviewState<PairUndo>());
  const [dialog, setDialog] = useState<"help" | "reject-remaining" | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const recordIds = useRef(0);

  const setSearch = (patch: Partial<ReferenceSearch>) =>
    void navigate({ search: (prev) => ({ ...prev, ...patch }) });

  const { data, hasNextPage, isFetchingNextPage, fetchNextPage, isPending, isError, error } = useInfiniteQuery({
    queryKey: [
      ...queryKeys.referenceItems,
      reference ?? null,
      status,
      site ?? null,
      hideRemoved,
      bands ?? null,
      includeLow,
      decidedBy ?? null,
    ],
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/v1/review/references/items", {
          params: {
            query: {
              status,
              reference_id: reference,
              site_id: site,
              cursor: pageParam,
              limit: PAGE_SIZE,
              hide_removed: hideRemoved,
              band: bands,
              include_low: includeLow,
              decided_by: decidedBy,
            },
          },
        }),
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    // Keep positions stable while reviewing; a fresh visit refetches.
    staleTime: Infinity,
    gcTime: 0,
  });

  const items = useMemo(() => {
    // With status "all", a pair decided meanwhile sorts after the proposed ones and can come back
    // on a later page: keep its first position.
    const seen = new Set<string>();
    const unique: ReferenceItem[] = [];
    for (const item of data?.pages.flatMap((page) => page.items) ?? []) {
      if (seen.has(item.key)) continue;
      seen.add(item.key);
      unique.push(item);
    }
    return unique;
  }, [data]);
  const meta = data?.pages[0]?.meta;
  const total = items.length;
  const current: ReferenceItem | undefined = items[state.index];

  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && state.index >= total - PREFETCH_MARGIN) {
      void fetchNextPage();
    }
  }, [hasNextPage, isFetchingNextPage, state.index, total, fetchNextPage]);

  const refreshCounts = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.counts });
    void queryClient.invalidateQueries({ queryKey: queryKeys.referenceSummaries });
  };

  const decide = (decision: FinalDecision) => {
    const selected = items.filter((item) => state.selected.includes(item.key));
    const bulk = selected.length > 0;
    const targets = bulk ? selected : current ? [current] : [];
    if (targets.length === 0) return;
    const id = ++recordIds.current;
    const previous: Record<string, ItemStatus> = Object.fromEntries(
      targets.map((item) => [item.key, statusOf(state, item)]),
    );
    dispatch({ type: "decided", id, keys: targets.map((item) => item.key), decision, previous, advance: !bulk, total });
    sendPairDecision(targets, decision).then(
      (undo) => {
        dispatch({ type: "settled", id, undo });
        refreshCounts();
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
    sendReset(record.undo).then(refreshCounts, (reason: unknown) =>
      setFlash(`Undo failed: ${errorMessage(reason)}`),
    );
  };

  const resetItem = (item: ReferenceItem) => {
    sendReset({ leakIds: item.leak ? [item.leak.id] : [], matchIds: item.match ? [item.match.id] : [] }).then(
      () => {
        dispatch({ type: "setStatus", keys: [item.key], status: "proposed" });
        refreshCounts();
      },
      (reason: unknown) => setFlash(errorMessage(reason)),
    );
  };

  const recheckPage = (pageId: number) => {
    unwrap(api.POST("/api/v1/review/pages/recheck", { body: { ids: [pageId] } })).then(
      (result) => setNotice(recheckMessage(result)),
      (reason: unknown) => setFlash(errorMessage(reason)),
    );
  };

  const jumpToReference = (slot: number) => {
    if (slot === 0) {
      setSearch({ reference: undefined });
      return;
    }
    const target = summaries.data?.[slot - 1];
    if (target) setSearch({ reference: target.id });
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
        return dispatch({ type: "next", total });
      case "back":
        return dispatch({ type: "prev" });
      case "undo":
        return undo();
      case "open":
        if (current) openExternal(current.page.url);
        return;
      case "toggleSelect":
        if (current) dispatch({ type: "toggleSelect", key: current.key });
        return;
      case "help":
        return setDialog("help");
      case "escape":
        setFlash(null);
        return dispatch({ type: "clearSelection" });
      case "reference":
        return jumpToReference(command.slot);
      case "band":
        return setSearch({ band: cycleBands(bands) });
    }
  });

  const activeReference = reference ?? current?.reference.id;
  const activeSummary = summaries.data?.find((summary) => summary.id === activeReference);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-display text-display-m">Reference leaks &amp; matches</h1>
        <label className="text-body-s">
          <span className="sr-only">Reference</span>
          <select
            className="max-w-md rounded-sm border border-border-control bg-surface-100 px-2 py-1.5"
            value={reference ?? ""}
            onChange={(event) =>
              setSearch({ reference: event.target.value ? Number(event.target.value) : undefined })
            }
          >
            <option value="">0 · All references</option>
            {(summaries.data ?? []).map((summary, index) => (
              <option key={summary.id} value={summary.id}>
                {index < 9 ? `${index + 1} · ` : ""}
                {creatorLabel(summary.creator_names)} — {summary.title} ({summary.proposed} open)
              </option>
            ))}
          </select>
        </label>
        <label className="text-body-s">
          <span className="sr-only">Status</span>
          <select
            className="rounded-sm border border-border-control bg-surface-100 px-2 py-1.5"
            value={status}
            onChange={(event) => setSearch({ status: event.target.value as StatusFilter })}
          >
            {STATUS_FILTERS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <BandFilter
          bands={bands}
          includeLow={includeLow}
          onBands={(next) => setSearch({ band: next })}
          onIncludeLow={(next) => setSearch({ includeLow: next ? true : undefined })}
        />
        <label className="text-body-s">
          <span className="sr-only">Decided by</span>
          <select
            className="rounded-sm border border-border-control bg-surface-100 px-2 py-1.5"
            value={decidedBy ?? ""}
            onChange={(event) => {
              const next = parseDecidedBy(event.target.value);
              // Decided pairs are never "proposed": switch to all statuses instead of an empty queue.
              setSearch({ decidedBy: next, ...(next && status === "proposed" ? { status: "all" as const } : {}) });
            }}
          >
            <option value="">decided by anyone</option>
            {DECIDED_BY.map((value) => (
              <option key={value} value={value}>
                decided by {value}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-body-s">
          <input
            type="checkbox"
            checked={hideRemoved}
            onChange={(event) => setSearch({ hideRemoved: event.target.checked ? true : undefined })}
          />
          Hide removed pages
        </label>
        <span className="text-body-s text-ink-muted" aria-live="polite">
          {Math.min(state.index + 1, total)} / {total}
          {hasNextPage ? "+" : ""}
        </span>
        <div className="ml-auto flex gap-2">
          <Button onClick={undo} disabled={!undoable(state)} shortcut="U">
            Undo
          </Button>
          <Button onClick={() => setDialog("reject-remaining")} disabled={!activeSummary || activeSummary.proposed === 0}>
            Reject remaining…
          </Button>
          <Button onClick={() => setDialog("help")} shortcut="?">
            Shortcuts
          </Button>
        </div>
      </div>

      {meta && meta.hidden_low > 0 ? (
        <p className="text-body-s text-ink-muted">
          {meta.hidden_low} low-confidence pair{meta.hidden_low === 1 ? "" : "s"} hidden.{" "}
          <button type="button" className="text-brand-ink underline" onClick={() => setSearch({ includeLow: true })}>
            Include low
          </button>
        </p>
      ) : null}

      <Flash message={flash} onClose={() => setFlash(null)} />
      <Flash message={notice} onClose={() => setNotice(null)} tone="info" />

      {state.selected.length > 0 ? (
        <div className="flex items-center gap-3 rounded-sm border border-hairline-strong bg-surface-200 px-3 py-2 text-body-s">
          <strong>{state.selected.length} selected</strong>
          <span className="text-ink-muted">Y / N apply to the selection.</span>
          <Button tone="confirm" onClick={() => decide("confirm")}>
            Confirm selected
          </Button>
          <Button tone="reject" onClick={() => decide("reject")}>
            Reject selected
          </Button>
          <Button onClick={() => dispatch({ type: "clearSelection" })}>Clear</Button>
        </div>
      ) : null}

      {isPending ? (
        <p className="text-ink-subtle">Loading queue…</p>
      ) : isError ? (
        <p className="text-critical-ink">Could not load the queue: {errorMessage(error)}</p>
      ) : current ? (
        <ItemView
          item={current}
          status={statusOf(state, current)}
          selected={state.selected.includes(current.key)}
          mode={mode}
          thresholds={meta?.band_thresholds}
          onDecide={decide}
          onSkip={() => dispatch({ type: "next", total })}
          onReset={() => resetItem(current)}
          onRecheck={() => recheckPage(current.page.id)}
        />
      ) : (
        <div className="rounded-md border border-dashed border-hairline-strong bg-surface-100 p-10 text-center text-ink-muted">
          {isFetchingNextPage ? "Loading more…" : total === 0 ? "Nothing to review here." : "End of the queue. Press ← to go back."}
        </div>
      )}

      <QueueList items={items} state={state} dispatch={dispatch} />

      {dialog === "help" ? (
        <ShortcutsDialog
          onClose={() => setDialog(null)}
          extra="Decisions apply to the whole (reference, page) pair: the suggestion and the fingerprint match stay consistent. B cycles the band filter; low-band matches stay hidden unless you include them."
        />
      ) : null}
      {dialog === "reject-remaining" && activeSummary ? (
        <RejectRemainingDialog
          summary={activeSummary}
          site={site}
          onClose={() => setDialog(null)}
          onDone={(undoPayload) => {
            const keys = items
              .filter(
                (item) =>
                  (item.leak && undoPayload.leakIds.includes(item.leak.id)) ||
                  (item.match && undoPayload.matchIds.includes(item.match.id)),
              )
              .map((item) => item.key);
            const id = ++recordIds.current;
            const previous = Object.fromEntries(keys.map((key) => [key, "proposed" as const]));
            dispatch({ type: "decided", id, keys, decision: "reject", previous, advance: false, total });
            dispatch({ type: "settled", id, undo: undoPayload });
            setDialog(null);
            refreshCounts();
          }}
        />
      ) : null}
    </div>
  );
}

function BandFilter({
  bands,
  includeLow,
  onBands,
  onIncludeLow,
}: {
  bands: Band[] | undefined;
  includeLow: boolean;
  onBands: (bands: Band[] | undefined) => void;
  onIncludeLow: (includeLow: boolean) => void;
}) {
  return (
    <div role="group" aria-label="Confidence band filter" className="flex flex-wrap items-center gap-1 text-body-s">
      {BANDS.map((band) => {
        const pressed = bands?.includes(band) ?? false;
        return (
          <button
            key={band}
            type="button"
            aria-pressed={pressed}
            onClick={() => onBands(toggleBand(bands, band))}
            className={`rounded-full border px-2.5 py-1 text-caption ${
              pressed ? `${bandClasses(band)} font-semibold` : "border-border-control bg-surface-100 text-ink-muted hover:bg-surface-300"
            }`}
          >
            {band}
          </button>
        );
      })}
      <label className="ml-1 flex items-center gap-1 text-caption text-ink-muted">
        <input type="checkbox" checked={includeLow} onChange={(event) => onIncludeLow(event.target.checked)} />
        include low
      </label>
      <span className="ml-1 text-caption text-ink-subtle" title="B cycles the band filter">
        {bandFilterText(bands, includeLow)} · <kbd className="font-mono">B</kbd>
      </span>
    </div>
  );
}

function BandBadge({ band, confidence }: { band: Band; confidence: number }) {
  return (
    <span
      className={`inline-block rounded-sm border px-1.5 py-0.5 text-caption font-medium whitespace-nowrap ${bandClasses(band)}`}
      title={`Confidence ${confidence.toFixed(4)} (band ${band})`}
    >
      {bandLabel(band, confidence)}
    </span>
  );
}

function ConfidencePanel({ match, thresholds }: { match: ReferenceMatch; thresholds: BandThresholds | undefined }) {
  const rows = signalBreakdown(match.confidence_signals);
  const sum = signalTotal(match.confidence_signals);
  return (
    <details className="mt-3 rounded-none border border-hairline bg-surface-100 p-2 text-body-s">
      <summary className="cursor-pointer font-medium">Why this confidence?</summary>
      {rows.length === 0 ? (
        <p className="mt-2 text-ink-muted">No stored signals (matched before confidence scoring).</p>
      ) : (
        <table className="mt-2 w-full text-left text-caption">
          <thead className="text-ink-subtle">
            <tr>
              <th className="py-1 pr-2 font-medium">Signal</th>
              <th className="py-1 text-right font-medium">Adds</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key} className="border-t border-hairline align-top">
                <td className="py-1 pr-2">
                  <span className="font-medium">{row.label}</span>
                  {row.value ? <span className="block text-ink-muted">{row.value}</span> : null}
                  {row.weight ? <span className="block text-ink-subtle">{row.weight}</span> : null}
                </td>
                <td className="py-1 text-right font-mono tabular-nums whitespace-nowrap">{row.contribution}</td>
              </tr>
            ))}
            <tr className="border-t border-hairline-strong">
              <td className="py-1 pr-2 font-medium">Confidence (sum, clamped to 0–1)</td>
              <td className="py-1 text-right font-mono tabular-nums">
                {match.confidence.toFixed(2)}
                {Math.abs(sum - match.confidence) > 0.005 ? ` (sum ${sum.toFixed(2)})` : ""}
              </td>
            </tr>
          </tbody>
        </table>
      )}
      {thresholds ? <p className="mt-2 text-caption text-ink-muted">Bands: {thresholdsText(thresholds)}</p> : null}
      {match.timeline_reason ? <p className="mt-1 text-caption text-ink-muted">Timeline: {match.timeline_reason}</p> : null}
    </details>
  );
}

export function ItemView({
  item,
  status,
  selected,
  mode,
  thresholds,
  onDecide,
  onSkip,
  onReset,
  onRecheck,
}: {
  item: ReferenceItem;
  status: ItemStatus;
  selected: boolean;
  mode: ImageMode;
  thresholds: BandThresholds | undefined;
  onDecide: (decision: FinalDecision) => void;
  onSkip: () => void;
  onReset: () => void;
  onRecheck: () => void;
}) {
  const { reference, page, signals, match, leak, cluster } = item;
  const notes = match ? evidenceNotes(match) : [];
  const articleRef = useRef<HTMLElement>(null);
  useEffect(() => {
    // Every decision (and Skip/Back) swaps in a new item: move accessible focus to it without
    // letting the browser's own focus-scroll fight the deliberate scroll on the next line, then
    // put the page back at the top so the next headline is visible (issue: the queue kept
    // scrolling down after a decision because QueueList's own scrollIntoView dragged the whole
    // `<main>` with it).
    const article = articleRef.current;
    article?.focus({ preventScroll: true });
    article?.closest("main")?.scrollTo({ top: 0 });
  }, [item.key]);
  return (
    <article
      ref={articleRef}
      tabIndex={-1}
      aria-label={`Review ${item.key}`}
      className={`rounded-md border p-5 shadow-sm ${selected ? "border-brand-ink bg-selection" : "border-hairline bg-surface-100"}`}
    >
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <StatusBadge status={status} />
        {match ? <BandBadge band={match.band} confidence={match.confidence} /> : <Badge>no match · no confidence</Badge>}
        {selected ? <Badge tone="accent">selected</Badge> : null}
        {match ? <Badge tone={match.strength === "strong" ? "accent" : "neutral"}>{match.strength} match</Badge> : null}
        {match && match.copy_extent !== "unknown" ? (
          <Badge tone={copyExtentTone(match.copy_extent)}>{copyExtentLabel(match.copy_extent)}</Badge>
        ) : null}
        {match?.decided_by === "auto" ? <Badge tone="caution">decided automatically</Badge> : null}
        {leak ? <Badge tone={leak.source === "cluster" ? "accent" : "neutral"}>source: {leak.source}</Badge> : null}
        <div className="ml-auto flex flex-wrap gap-2">
          <Button tone="confirm" onClick={() => onDecide("confirm")} shortcut="Y">
            Confirm leak
          </Button>
          <Button tone="reject" onClick={() => onDecide("reject")} shortcut="N">
            Not a leak
          </Button>
          <Button onClick={onSkip} shortcut="S">
            Skip
          </Button>
          <Button onClick={() => openExternal(page.url)} shortcut="O">
            Open page
          </Button>
          {status !== "proposed" ? <Button onClick={onReset}>Reset decision</Button> : null}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(15rem,20rem)_minmax(0,1fr)]">
        <section aria-label="Reference" className="min-w-0">
          <h2 className="text-overline uppercase text-ink-subtle">Original</h2>
          <p className="mt-1 text-body-s text-ink-muted">{creatorLabel(reference.creator_names)}</p>
          <p className="font-display text-display-s leading-tight">{reference.title}</p>
          <dl className="mt-3 space-y-1">
            <Field label="Duration">{clock(reference.duration_s)}</Field>
            <Field label="Reference id">{reference.id}</Field>
          </dl>
        </section>

        <section aria-label="Signals" className="min-w-0 rounded-none border border-hairline bg-surface-inset p-3">
          <h2 className="text-overline uppercase text-ink-subtle">Signals</h2>
          {match ? (
            <p className="mt-1 flex items-baseline gap-2">
              <span className="text-3xl font-bold tabular-nums">{percent(match.confidence)}</span>
              <span className="text-body-s text-ink-muted">confidence</span>
            </p>
          ) : null}
          <p className="mt-1 text-body-s text-ink-muted">
            ranking score <span className="font-semibold tabular-nums">{signals.score.toFixed(2)}</span>
          </p>
          <dl className="mt-2 space-y-1">
            {match ? <Field label="Timeline">{timelineText(match)}</Field> : null}
            {match ? <Field label="Copy extent">{copyExtentText(match)}</Field> : null}
            <Field label="Duration diff">
              {signedSeconds(signals.duration_diff_s)}
              {signals.duration_ratio !== null ? ` (${percent(signals.duration_ratio)})` : ""}
            </Field>
            <Field label="Title similarity">{percent(signals.title_similarity)}</Field>
            <Field label="Creator linked">{signals.creator_linked ? "yes" : "no"}</Field>
            {signals.related_kind ? (
              <Field label="Propagated">
                {signals.related_kind} of page #{signals.related_page_id ?? "?"}
              </Field>
            ) : null}
            {match ? (
              <>
                {match.candidate_source === "embedding" ? (
                  <Field label="Source">
                    second signal only — no fingerprint evidence; judge the pictures
                  </Field>
                ) : null}
                <Field label="Best distance">{match.best_distance}</Field>
                <Field label="Evidence frames">{match.evidence_frames}</Field>
                <Field label="Matched / page">
                  {match.matched_frames} / {match.page_frames}
                </Field>
                <Field label="Best moment">{match.best_moment}</Field>
                <Field label="Matched moments">{match.matched_moments.join(", ") || "—"}</Field>
                <Field label="Shared only">{match.shared_moments.join(", ") || "—"}</Field>
              </>
            ) : (
              <Field label="Fingerprint">no match</Field>
            )}
            {leak ? <Field label="Stored score">{leak.score.toFixed(2)}</Field> : null}
          </dl>
          {notes.length ? (
            <ul aria-label="Evidence notes" className="mt-2 space-y-1 text-caption text-ink-muted">
              {notes.map((note) => (
                <li key={note} className="rounded-sm border border-caution-ink bg-caution-quiet px-2 py-1">
                  {note}
                </li>
              ))}
            </ul>
          ) : null}
          {match ? <ConfidencePanel match={match} thresholds={thresholds} /> : null}
        </section>

        <section aria-label="Page" className={`min-w-0 ${isRemoved(page) ? "opacity-60" : ""}`}>
          <h2 className="text-overline uppercase text-ink-subtle">Page · {page.site}</h2>
          <p className={`mt-1 font-display text-display-s leading-tight ${isRemoved(page) ? "line-through" : ""}`}>
            {page.title || "(no title)"}
          </p>
          <dl className="mt-3 space-y-1">
            <Field label="URL">
              <ExternalLink url={page.url} />
            </Field>
            <Field label="Duration">{clock(page.duration_s)}</Field>
            <Field label="Availability">
              <span className="flex flex-wrap items-center gap-2">
                <AvailabilityBadge page={page} />
                <button type="button" className="text-caption text-brand-ink underline" onClick={onRecheck}>
                  Check availability now
                </button>
              </span>
            </Field>
            <Field label="Uploaded">{dateTime(page.uploaded_at)}</Field>
            <Field label="Cluster">{cluster ? `#${cluster.id} · ${cluster.member_count} members` : "—"}</Field>
            {page.safety_status !== "clear" ? <Field label="Safety">{page.safety_status}</Field> : null}
          </dl>
        </section>
      </div>

      <div className="mt-5">
        <MediaGrid media={item.media} hidden={item.media_hidden} mode={mode} />
      </div>
    </article>
  );
}

export function QueueList({
  items,
  state,
  dispatch,
}: {
  items: ReferenceItem[];
  state: ReviewState<PairUndo>;
  dispatch: React.Dispatch<ReviewAction<PairUndo>>;
}) {
  const container = useRef<HTMLDivElement>(null);
  const currentRow = useRef<HTMLTableRowElement>(null);
  useEffect(() => {
    // This is a small navigator list nested inside the page's own scrolling `<main>`. A native
    // `element.scrollIntoView()` walks every scrollable ancestor, not just this box, so it used to
    // scroll `<main>` itself to bring a row here into view — dragging the whole queue down after
    // every decision even though the row was already reachable inside this list. Move only this
    // box's own scrollTop, never an ancestor's.
    const box = container.current;
    const row = currentRow.current;
    if (!box || !row) return;
    const rowTop = row.offsetTop;
    const rowBottom = rowTop + row.offsetHeight;
    const viewTop = box.scrollTop;
    const viewBottom = viewTop + box.clientHeight;
    if (rowTop < viewTop) box.scrollTop = rowTop;
    else if (rowBottom > viewBottom) box.scrollTop = rowBottom - box.clientHeight;
  }, [state.index]);
  const order = items.map((item) => item.key);
  if (items.length === 0) return null;
  return (
    <div ref={container} className="max-h-80 overflow-auto rounded-none border border-hairline bg-surface-100">
      <table className="w-full text-left text-body-s">
        <thead className="sticky top-0 bg-surface-200 text-overline uppercase text-ink-muted">
          <tr>
            <th className="w-10 px-2 py-1.5">
              <span className="sr-only">Select</span>
            </th>
            <th className="px-2 py-1.5">#</th>
            <th className="px-2 py-1.5">Status</th>
            <th className="px-2 py-1.5">Band</th>
            <th className="px-2 py-1.5">Original</th>
            <th className="px-2 py-1.5">Page</th>
            <th className="px-2 py-1.5">Site</th>
            <th className="px-2 py-1.5">Availability</th>
            <th className="px-2 py-1.5 text-right">Score</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item, index) => {
            const isCurrent = index === state.index;
            return (
              <tr
                key={item.key}
                ref={isCurrent ? currentRow : undefined}
                aria-current={isCurrent ? "true" : undefined}
                className={`cursor-pointer border-t border-hairline ${isCurrent ? "bg-selection" : "hover:bg-surface-200"} ${isRemoved(item.page) ? "opacity-50" : ""}`}
                onClick={() => dispatch({ type: "goto", index, total: items.length })}
              >
                <td className="px-2 py-1">
                  <input
                    type="checkbox"
                    aria-label={`Select ${item.page.title || item.key}`}
                    checked={state.selected.includes(item.key)}
                    onClick={(event) => {
                      event.stopPropagation();
                      dispatch(
                        event.shiftKey
                          ? { type: "selectRange", order, key: item.key }
                          : { type: "toggleSelect", key: item.key },
                      );
                    }}
                    onChange={() => undefined}
                  />
                </td>
                <td className="px-2 py-1 tabular-nums text-ink-subtle">{index + 1}</td>
                <td className="px-2 py-1">
                  <StatusBadge status={statusOf(state, item)} />
                </td>
                <td className="px-2 py-1">
                  {item.match ? (
                    <BandBadge band={item.match.band} confidence={item.match.confidence} />
                  ) : (
                    <span className="text-caption text-ink-subtle">no match</span>
                  )}
                </td>
                <td className="max-w-48 truncate px-2 py-1">{item.reference.title}</td>
                <td className="max-w-80 truncate px-2 py-1">{item.page.title || item.page.url}</td>
                <td className="px-2 py-1 text-ink-muted">{item.page.site}</td>
                <td className="px-2 py-1 whitespace-nowrap">
                  <AvailabilityBadge page={item.page} />
                </td>
                <td className="px-2 py-1 text-right tabular-nums">{item.signals.score.toFixed(2)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RejectRemainingDialog({
  summary,
  site,
  onClose,
  onDone,
}: {
  summary: ReferenceSummary;
  site: number | undefined;
  onClose: () => void;
  onDone: (undo: PairUndo) => void;
}) {
  const [includeMatches, setIncludeMatches] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const withMatches = summary.proposed - summary.proposed_suggestions;
  const count = includeMatches ? summary.proposed : summary.proposed_suggestions;
  const submit = () => {
    setBusy(true);
    unwrap(
      api.POST("/api/v1/review/references/{reference_id}/reject-remaining", {
        params: { path: { reference_id: summary.id } },
        body: { include_matches: includeMatches, site_id: site ?? null },
      }),
    ).then(
      (result) => onDone({ leakIds: result.leak_ids, matchIds: result.match_ids }),
      (reason: unknown) => {
        setBusy(false);
        setProblem(errorMessage(reason));
      },
    );
  };
  return (
    <Dialog title="Reject remaining suggestions" onClose={onClose}>
      <p className="text-body-s">
        {creatorLabel(summary.creator_names)} — <strong>{summary.title}</strong>
      </p>
      <p className="mt-2 text-body-s">
        Rejects <strong>{count}</strong> still-proposed pair{count === 1 ? "" : "s"}
        {site ? " on the filtered site" : ""}. Decided pairs stay untouched. Undo (U) reverts it.
      </p>
      {withMatches > 0 ? (
        <label className="mt-3 flex items-center gap-2 text-body-s">
          <input type="checkbox" checked={includeMatches} onChange={(event) => setIncludeMatches(event.target.checked)} />
          Also reject {withMatches} pair{withMatches === 1 ? "" : "s"} with a fingerprint match
        </label>
      ) : null}
      {problem ? (
        <p role="alert" className="mt-3 text-body-s text-critical-ink">
          {problem}
        </p>
      ) : null}
      <div className="mt-5 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button tone="reject" onClick={submit} disabled={busy || count === 0}>
          Reject {count}
        </Button>
      </div>
    </Dialog>
  );
}
