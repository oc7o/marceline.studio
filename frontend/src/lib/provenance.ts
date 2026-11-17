/**
 * Where a cluster member's status came from, and how a cluster links to creator originals.
 *
 * The database records no decider for cluster members. The API derives `provenance` from the
 * evidence field and SAME pair labels; labels here say so. Nothing is ever auto-confirmed:
 * `build_clusters` only proposes, and imports the operator's SAME pair labels as confirmed.
 */

import { copyExtentLabel, type CopyExtent } from "./copyExtent";
import { creatorLabel } from "./format";

export type Provenance = "system" | "pair_label" | "reviewer";

export const EVIDENCE_LABELS: Readonly<Record<string, string>> = {
  pdq: "PDQ",
  sha256: "SHA256",
  external_link: "external link",
  mirror: "mirror poster",
  manual: "manual",
};

export function evidenceLabel(evidence: string): string {
  return EVIDENCE_LABELS[evidence] ?? evidence;
}

const capitalize = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);

/**
 * `status` is what the screen shows (it may be an optimistic decision from this session);
 * `serverStatus` and `provenance` are what the API reported when the queue loaded.
 */
export function provenanceLabel(member: {
  status: string;
  serverStatus: string;
  provenance: string;
  evidence: string;
}): string {
  const { status, serverStatus, provenance, evidence } = member;
  if (status !== serverStatus) {
    return status === "proposed" ? "Proposed — reset by you (this session)" : `${capitalize(status)} — by you (this session)`;
  }
  if (status === "proposed") return `Proposed by system — ${evidenceLabel(evidence)}`;
  if (status === "confirmed" && provenance === "pair_label") return "Confirmed — from your pair labels";
  const origin = evidence === "manual" ? "added manually" : `system proposed via ${evidenceLabel(evidence)}`;
  return `${capitalize(status)} — by you (${origin})`;
}

export const STATUS_LEGEND: ReadonlyArray<{ status: string; text: string }> = [
  {
    status: "proposed",
    text: "Suggested by build_clusters from evidence (PDQ frames, SHA256, external links, mirror posters). Nobody has decided yet.",
  },
  {
    status: "confirmed",
    text: "A person decided it is the same video: in the admin, in this screen, or through a SAME pair label you created (imported as confirmed with evidence “manual”).",
  },
  {
    status: "rejected",
    text: "A person decided it does not belong in this cluster. Rejections are kept so the page is not proposed again.",
  },
];

export const LEGEND_NOTES: readonly string[] = [
  "Nothing is auto-confirmed. The system only proposes.",
  "“From your pair labels” / “by you” is derived from the evidence field and your pair labels; the database does not store who decided.",
  "Reset puts a member back to proposed. A member confirmed from a SAME pair label is confirmed again on the next build_clusters run unless you change that label in the admin.",
  "A cluster groups copies of one pirate video. Most clusters are not linked to any of your originals.",
  "“full copy”, “excerpt” and “possible compilation” on a match say how much of the original the page carries, measured from its matched frames. They never hide a member: clips stay in their video's cluster.",
];

interface LinkEvidence {
  status: string;
  match_strength: string | null;
  leak_status: string | null;
  leak_source: string | null;
  /** The match's copy extent (issue #62); null or absent without a match. */
  copy_extent?: CopyExtent | null;
}

/** "strong match", "confirmed · weak match · excerpt", "proposed leak via cluster", "rejected: not a leak".
 * A match's copy extent is added once it is determined; "extent unknown" is left out. */
export function linkEvidence(link: LinkEvidence): string {
  if (link.status === "rejected") return "rejected: not a leak";
  const decided = link.status === "confirmed" || link.status === "conflict" ? `${link.status} · ` : "";
  if (link.match_strength) {
    const extent = link.copy_extent && link.copy_extent !== "unknown" ? ` · ${copyExtentLabel(link.copy_extent)}` : "";
    return `${decided}${link.match_strength} match${extent}`;
  }
  const via = link.leak_source === "cluster" ? " via cluster" : link.leak_source ? ` (${link.leak_source})` : "";
  return `${decided}${decided ? "" : `${link.leak_status ?? link.status} `}leak${via}`;
}

/** "Thea Lindqvist — Winter Drive (strong match on page 48, +2 more pages)".
 * Originals with several creators read "Sasha Meyer + Carla Bremer — Title". */
export function linkedOriginalSummary(original: {
  reference: { creator_names: readonly string[]; title: string };
  pages: ReadonlyArray<LinkEvidence & { page_id: number }>;
}): string {
  const name = `${creatorLabel(original.reference.creator_names)} — ${original.reference.title}`;
  const [best, ...rest] = original.pages;
  if (!best) return name;
  const more = rest.length ? `, +${rest.length} more page${rest.length === 1 ? "" : "s"}` : "";
  return `${name} (${linkEvidence(best)} on page ${best.page_id}${more})`;
}
