import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useReducer, useRef, useState } from "react";

import { type Schemas, api, errorMessage, unwrap } from "../api/client";
import { queryKeys } from "../api/queries";
import { Button, Dialog, Flash } from "../components/ui";
import {
  ACTION_STATUS,
  CANDIDATE_SHORTCUTS,
  type CandidateAction,
  candidateKeyToCommand,
  candidateReducer,
  initialCandidateState,
  targetIds,
} from "../lib/candidateKeyboard";
import { dateTime } from "../lib/format";
import { count, domainHref, queryLine, robotsLabel, usd } from "../lib/overviewFormat";
import { overviewQueryKey } from "./OverviewScreen";

type Candidate = Schemas["CandidateOut"];
type StatusFilter = "proposed" | "approved" | "rejected" | "hoster" | "ignored" | "all";
type BlockedFilter = "any" | "yes" | "no";

const STATUSES: StatusFilter[] = ["proposed", "approved", "rejected", "hoster", "ignored", "all"];
const KINDS = ["tube", "file_hoster", "forum", "social", "unknown"];

const STATUS_STYLES: Record<string, string> = {
  proposed: "bg-surface-300 text-ink",
  approved: "bg-positive-quiet text-positive-ink",
  rejected: "bg-critical-quiet text-critical-ink",
  hoster: "bg-accent-quiet text-accent",
  ignored: "bg-surface-200 text-ink-muted",
};

interface Filters {
  status: StatusFilter;
  kind: string;
  cms: string;
  blocked: BlockedFilter;
}

/**
 * Discovery review (ADR 0013). Everything about a candidate is text: nothing is loaded from its
 * domain. The domain is a plain link that only opens when clicked.
 */
export function CandidatesScreen() {
  const [filters, setFilters] = useState<Filters>({ status: "proposed", kind: "", cms: "", blocked: "any" });
  const update = (patch: Partial<Filters>) => setFilters((current) => ({ ...current, ...patch }));
  const key = `${filters.status}-${filters.kind}-${filters.cms}-${filters.blocked}`;
  return (
    <div className="flex flex-col gap-4">
      <CandidateQueue key={key} filters={filters} onFilters={update} />
      <SearchCallsPanel />
    </div>
  );
}

function CandidateQueue({ filters, onFilters }: { filters: Filters; onFilters: (patch: Partial<Filters>) => void }) {
  const queryClient = useQueryClient();
  const [state, dispatch] = useReducer(candidateReducer, initialCandidateState);
  const [flash, setFlash] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [help, setHelp] = useState(false);
  const rows = useRef(new Map<number, HTMLLIElement>());
  const query = useQuery({
    queryKey: ["candidates", filters],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/review/discovery/candidates", {
          params: {
            query: {
              status: filters.status,
              kind: filters.kind || undefined,
              cms_guess: filters.cms || undefined,
              blocked: filters.blocked === "any" ? undefined : filters.blocked === "yes",
            },
          },
        }),
      ),
    staleTime: Infinity,
    gcTime: 0,
  });
  const items = query.data?.items;

  useEffect(() => {
    if (items) dispatch({ type: "load", ids: items.map((item) => item.id) });
  }, [items]);

  const currentId = state.ids[state.index];
  useEffect(() => {
    if (currentId !== undefined) rows.current.get(currentId)?.scrollIntoView?.({ block: "nearest" });
  }, [currentId]);

  const decide = (action: CandidateAction) => {
    const ids = targetIds(state);
    if (ids.length === 0) return;
    const previous = Object.fromEntries(ids.map((id) => [id, state.statuses[id]]));
    dispatch({ type: "decided", ids, status: ACTION_STATUS[action] });
    unwrap(api.POST("/api/v1/review/discovery/candidates/decide", { body: { ids, action } })).then(
      (result) => {
        dispatch({ type: "settled", ids });
        if (result.created_site_ids.length) {
          setNotice(`Created ${result.created_site_ids.length} disabled site(s). Enable them and add adapters in the admin.`);
        } else if (result.removed_site_ids.length) {
          setNotice(`Approval undone: deleted ${result.removed_site_ids.length} unused disabled site(s).`);
        }
        void queryClient.invalidateQueries({ queryKey: overviewQueryKey });
        void queryClient.invalidateQueries({ queryKey: queryKeys.counts });
      },
      (error: unknown) => {
        dispatch({ type: "failed", ids, previous });
        setFlash(errorMessage(error));
      },
    );
  };

  const latest = useRef({ decide, help });
  useEffect(() => {
    latest.current = { decide, help };
  });
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.repeat && event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "j" && event.key !== "k") return;
      const command = candidateKeyToCommand(event);
      if (!command) return;
      event.preventDefault();
      switch (command.type) {
        case "decide":
          if (!latest.current.help) latest.current.decide(command.action);
          break;
        case "next":
        case "prev":
          dispatch({ type: command.type });
          break;
        case "toggleSelect":
          dispatch({ type: "toggleCurrent" });
          break;
        case "help":
          setHelp((open) => !open);
          break;
        case "escape":
          if (latest.current.help) setHelp(false);
          else dispatch({ type: "clearSelection" });
          break;
      }
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);

  const targets = targetIds(state);
  const data = query.data;
  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-display text-display-m">Source candidates</h1>
        {data ? (
          <span className="text-body-s text-ink-muted">
            {count(data.items.length)} of {count(data.total)} shown
          </span>
        ) : null}
        <div className="ml-auto flex flex-wrap items-center gap-2 text-body-s">
          <FilterSelect label="Status" value={filters.status} onChange={(value) => onFilters({ status: value as StatusFilter })}>
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {status}
                {data && status !== "all" ? ` (${data.status_counts[status] ?? 0})` : ""}
              </option>
            ))}
          </FilterSelect>
          <FilterSelect label="Kind" value={filters.kind} onChange={(value) => onFilters({ kind: value })}>
            <option value="">any</option>
            {KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {kind}
              </option>
            ))}
          </FilterSelect>
          <FilterSelect label="CMS" value={filters.cms} onChange={(value) => onFilters({ cms: value })}>
            <option value="">any</option>
            {(data?.cms_values ?? (filters.cms ? [filters.cms] : [])).map((cms) => (
              <option key={cms} value={cms}>
                {cms}
              </option>
            ))}
          </FilterSelect>
          <FilterSelect label="Blocked" value={filters.blocked} onChange={(value) => onFilters({ blocked: value as BlockedFilter })}>
            <option value="any">any</option>
            <option value="yes">blocked</option>
            <option value="no">not blocked</option>
          </FilterSelect>
        </div>
      </div>

      <div className="sticky top-0 z-10 flex flex-wrap items-center gap-2 rounded-sm border border-hairline bg-surface-100 p-2">
        <span className="text-body-s text-ink-muted">
          {state.selected.length ? `${state.selected.length} selected` : "Current row"}
        </span>
        <Button tone="confirm" shortcut="A" onClick={() => decide("approve")} disabled={targets.length === 0}>
          Approve
        </Button>
        <Button tone="reject" shortcut="R" onClick={() => decide("reject")} disabled={targets.length === 0}>
          Reject
        </Button>
        <Button shortcut="H" onClick={() => decide("hoster")} disabled={targets.length === 0}>
          Hoster
        </Button>
        <Button shortcut="I" onClick={() => decide("ignore")} disabled={targets.length === 0}>
          Ignore
        </Button>
        <Button shortcut="P" onClick={() => decide("reset")} disabled={targets.length === 0}>
          Back to proposed
        </Button>
        <Button onClick={() => dispatch({ type: "selectAll" })} disabled={state.ids.length === 0}>
          Select all
        </Button>
        <Button onClick={() => void query.refetch()} disabled={query.isFetching}>
          Reload
        </Button>
        <span className="ml-auto text-caption text-ink-subtle">
          Approve creates a disabled Site; any other decision undoes that while the Site is unused. <kbd className="font-mono">?</kbd>{" "}
          shortcuts
        </span>
      </div>

      <Flash message={flash} onClose={() => setFlash(null)} />
      {notice ? (
        <div role="status" className="flex justify-between gap-4 rounded-sm border border-positive-ink bg-positive-quiet px-3 py-2 text-body-s text-positive-ink">
          <span>{notice}</span>
          <button type="button" onClick={() => setNotice(null)} className="font-semibold" aria-label="Dismiss">
            ×
          </button>
        </div>
      ) : null}

      {query.isPending ? (
        <p className="text-ink-subtle">Loading…</p>
      ) : query.isError ? (
        <p className="text-critical-ink">Could not load: {errorMessage(query.error)}</p>
      ) : query.data.items.length === 0 ? (
        <div className="rounded-md border border-dashed border-border-control bg-surface-100 p-10 text-center text-ink-muted">
          No candidates match these filters.
        </div>
      ) : (
        <ul className="flex flex-col gap-2">
          {query.data.items.map((item) => (
            <CandidateRow
              key={item.id}
              item={item}
              status={state.statuses[item.id] ?? item.status}
              current={item.id === currentId}
              checked={state.selected.includes(item.id)}
              pending={state.pending.includes(item.id)}
              onFocus={() => dispatch({ type: "focus", id: item.id })}
              onToggle={() => dispatch({ type: "toggle", id: item.id })}
              rowRef={(element) => {
                if (element) rows.current.set(item.id, element);
                else rows.current.delete(item.id);
              }}
            />
          ))}
        </ul>
      )}

      {help ? (
        <Dialog title="Keyboard shortcuts" onClose={() => setHelp(false)}>
          <table className="w-full text-body-s">
            <tbody>
              {CANDIDATE_SHORTCUTS.map((shortcut) => (
                <tr key={shortcut.keys} className="border-b border-hairline last:border-0">
                  <td className="py-1.5 pr-4 font-mono font-semibold whitespace-nowrap">{shortcut.keys}</td>
                  <td className="py-1.5">{shortcut.action}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-4 flex justify-end">
            <Button onClick={() => setHelp(false)}>Close</Button>
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  children,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  children: React.ReactNode;
}) {
  return (
    <label className="flex items-center gap-1">
      <span className="text-ink-muted">{label}</span>
      <select className="rounded-sm border border-border-control bg-surface-100 px-2 py-1" value={value} onChange={(event) => onChange(event.target.value)}>
        {children}
      </select>
    </label>
  );
}

function CandidateRow({
  item,
  status,
  current,
  checked,
  pending,
  onFocus,
  onToggle,
  rowRef,
}: {
  item: Candidate;
  status: string;
  current: boolean;
  checked: boolean;
  pending: boolean;
  onFocus: () => void;
  onToggle: () => void;
  rowRef: (element: HTMLLIElement | null) => void;
}) {
  const href = domainHref(item.domain);
  const evidence = item.evidence;
  return (
    <li
      ref={rowRef}
      onClick={onFocus}
      aria-current={current ? "true" : undefined}
      className={`rounded-md border p-3 ${current ? "border-brand-ink bg-selection" : checked ? "border-brand-ink bg-surface-100" : "border-hairline bg-surface-100"} ${
        pending ? "opacity-60" : ""
      }`}
    >
      <div className="flex gap-3">
        <input type="checkbox" checked={checked} onChange={onToggle} aria-label={`Select ${item.domain}`} className="mt-1" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            {href ? (
              <a href={href} target="_blank" rel="noopener noreferrer" className="font-semibold text-brand-ink underline">
                {item.domain}
              </a>
            ) : (
              <span className="font-semibold">{item.domain}</span>
            )}
            <span className={`rounded-sm px-2 py-0.5 text-caption font-semibold uppercase ${STATUS_STYLES[status] ?? "bg-surface-200"}`}>{status}</span>
            <span className="rounded-sm bg-surface-200 px-1.5 py-0.5 text-caption">{item.kind}</span>
            <span className="rounded-sm bg-surface-200 px-1.5 py-0.5 text-caption">cms {item.cms_guess}</span>
            {item.blocked_reason ? (
              <span className="rounded-sm bg-caution-quiet px-1.5 py-0.5 text-caption text-caution-ink">blocked: {item.blocked_reason}</span>
            ) : null}
          </div>
          {item.title ? <div className="mt-1 text-body-s break-words text-ink-muted">{item.title}</div> : null}
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-caption text-ink-muted">
            <span>{count(item.distinct_creators)} creators</span>
            <span>{count(item.search_hits)} search hits</span>
            <span>best rank {item.best_rank ?? "—"}</span>
            <span>{count(item.outlink_pages)} linking pages</span>
            <span>search {item.has_search ? "yes" : "no"}</span>
            <span>{count(item.video_links_seen)} video links</span>
            <span>robots {robotsLabel(item.robots_allows)}</span>
            <span>HTTP {item.http_status ?? "—"}</span>
            <span>probed {dateTime(item.probed_at)}</span>
          </div>
          {evidence.queries.length || evidence.linking_pages.length || evidence.redirects_to || evidence.mirror_of ? (
            <details className="mt-2 text-caption">
              <summary className="cursor-pointer text-ink-muted">
                Evidence: {evidence.queries.length} queries, {evidence.linking_pages.length} linking pages
              </summary>
              <div className="mt-1 flex flex-col gap-0.5 font-mono break-all text-ink-muted">
                {evidence.queries.map((sample, index) => (
                  <div key={`q${index}`}>{queryLine(sample)}</div>
                ))}
                {evidence.linking_pages.map((url, index) => (
                  <div key={`p${index}`}>linked from {url}</div>
                ))}
                {evidence.redirects_to ? <div>redirects to {evidence.redirects_to}</div> : null}
                {evidence.mirror_of ? <div>mirror of {evidence.mirror_of}</div> : null}
              </div>
            </details>
          ) : null}
          {item.notes ? <div className="mt-1 text-caption whitespace-pre-wrap text-ink-muted">Notes: {item.notes}</div> : null}
        </div>
      </div>
    </li>
  );
}

function SearchCallsPanel() {
  const query = useQuery({
    queryKey: ["search-calls"],
    queryFn: () => unwrap(api.GET("/api/v1/review/discovery/search-calls", { params: { query: { limit: 25 } } })),
  });
  return (
    <section className="rounded-md border border-hairline bg-surface-100 p-4">
      <h2 className="mb-2 font-semibold">Search calls</h2>
      {query.isPending ? (
        <p className="text-body-s text-ink-subtle">Loading…</p>
      ) : query.isError ? (
        <p className="text-body-s text-critical-ink">Could not load: {errorMessage(query.error)}</p>
      ) : (
        <>
          <p className="mb-2 text-body-s text-ink-muted">
            Total {usd(query.data.total_cost_usd)} over {count(query.data.total_calls)} calls · last 24 h{" "}
            {usd(query.data.cost_24h_usd)}
          </p>
          {query.data.calls.length === 0 ? (
            <p className="text-body-s text-ink-muted">No search calls yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-caption">
                <thead className="text-left text-ink-subtle">
                  <tr className="border-b border-hairline">
                    <th className="py-1 pr-3 font-medium">When</th>
                    <th className="py-1 pr-3 font-medium">Engine / mode</th>
                    <th className="py-1 pr-3 font-medium">Query</th>
                    <th className="py-1 pr-3 font-medium">Status</th>
                    <th className="py-1 pr-3 font-medium">Results</th>
                    <th className="py-1 font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {query.data.calls.map((call) => (
                    <tr key={call.id} className="border-b border-hairline align-top last:border-0">
                      <td className="py-1 pr-3 whitespace-nowrap">{dateTime(call.created_at)}</td>
                      <td className="py-1 pr-3">
                        {call.engine} / {call.mode}
                      </td>
                      <td className="py-1 pr-3 break-words">{call.query}</td>
                      <td className="py-1 pr-3" title={call.error || undefined}>
                        {call.status}
                        {call.error ? " (error)" : ""}
                      </td>
                      <td className="py-1 pr-3 tabular-nums">{call.result_count}</td>
                      <td className="py-1 tabular-nums">{usd(call.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}
