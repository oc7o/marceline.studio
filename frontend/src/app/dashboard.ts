/**
 * Pure catalogue-level aggregation for the customer overview (`/app`, issue #32): "what is the
 * state of my catalogue" across every original, not just one at a time.
 *
 * Nothing here talks to the network, and nothing here invents a fact: every number and every
 * event is derived from what the existing customer reads already send
 * (`GET /app/originals`, `/app/findings`, `/app/removals`, and — where it is served —
 * `/app/rights`, ADR 0034). No new matching or review logic; this module only reshapes rows the
 * API already returns for the per-original screens into a catalogue-wide answer.
 */
import type { CustomerFinding, CustomerOriginal, Removal } from "./apiTypes";
import type { RightsWork } from "./rights";

export interface CatalogueTotals {
  /** How many originals this organisation owns, whatever their state. */
  originals: number;
  /** Every human-reviewed matching copy across the catalogue (online and removed together). */
  confirmed: number;
  /** Of those, how many are removed. */
  removed: number;
}

/** Sums the per-original counts the overview already reads — no new counting rule. */
export function catalogueTotals(
  originals: readonly Pick<CustomerOriginal, "counts">[],
): CatalogueTotals {
  return originals.reduce<CatalogueTotals>(
    (totals, original) => ({
      originals: totals.originals + 1,
      confirmed: totals.confirmed + original.counts.confirmed,
      removed: totals.removed + original.counts.removed,
    }),
    { originals: 0, confirmed: 0, removed: 0 },
  );
}

export type RecentChangeKind = "confirmed" | "removed" | "reappeared";

export interface RecentChange {
  kind: RecentChangeKind;
  /** A day ("confirmed") or a moment (the other two) — whatever the API stamped it with. */
  at: string;
  originalId: string;
  title: string;
}

type FindingRow = Pick<CustomerFinding, "original_id" | "confirmed_on">;
type RemovalRow = Pick<Removal, "original_id" | "title" | "removed_at" | "reappeared_at" | "state">;

/**
 * The most recent catalogue-wide changes, newest first: a copy human-reviewed, one removed, one
 * back online. `limit` caps the list (default 5); a tie keeps the API's own order (findings, then
 * removals, each in the order they arrived) so the same input always renders the same way.
 *
 * A finding whose original is not in `originals` is skipped rather than shown with a blank title —
 * that should not happen (every finding belongs to an owned original), but this stays a pure
 * function that never renders half a fact.
 */
export function recentChanges(
  originals: readonly Pick<CustomerOriginal, "id" | "title">[],
  findings: readonly FindingRow[],
  removals: readonly RemovalRow[],
  limit = 5,
): RecentChange[] {
  const titles = new Map(originals.map((original) => [original.id, original.title]));
  const events: (RecentChange & { rank: number })[] = [];
  findings.forEach((finding, index) => {
    const title = titles.get(finding.original_id);
    if (title === undefined) return;
    events.push({
      kind: "confirmed",
      at: finding.confirmed_on,
      originalId: finding.original_id,
      title,
      rank: index,
    });
  });
  removals.forEach((removal, index) => {
    const reopened = removal.state === "reopened" && removal.reappeared_at !== null;
    events.push({
      kind: reopened ? "reappeared" : "removed",
      at: reopened ? (removal.reappeared_at as string) : removal.removed_at,
      originalId: removal.original_id,
      title: removal.title,
      rank: events.length + index,
    });
  });
  return events
    .sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime() || a.rank - b.rank)
    .slice(0, limit)
    .map(({ kind, at, originalId, title }) => ({ kind, at, originalId, title }));
}

export interface RightsAttention {
  originalId: string;
  title: string;
  blockers: readonly RightsWork["notice_blockers"][number][];
}

/**
 * Which originals something was found of still have a rights step in the way of a notice draft
 * (ADR 0034: `notice_blockers`). An original with nothing found is never listed here — there is no
 * next step yet to block. Silent (empty) when the rights overview carries no row for an original
 * (unassigned, or not served on this deployment): never invent a blocker.
 */
export function rightsAttentionFor(
  originals: readonly Pick<CustomerOriginal, "id" | "state">[],
  works: readonly RightsWork[],
): RightsAttention[] {
  const byId = new Map(works.map((work) => [work.original_id, work]));
  const attention: RightsAttention[] = [];
  for (const original of originals) {
    if (original.state === "nothing_found") continue;
    const work = byId.get(original.id);
    if (!work || work.notice_blockers.length === 0) continue;
    attention.push({ originalId: original.id, title: work.title, blockers: work.notice_blockers });
  }
  return attention;
}
