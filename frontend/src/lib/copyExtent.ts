/**
 * Copy extent display for staff (issue #62): full copy, excerpt, possible compilation or unknown.
 * Derived by matching (`matching/copy_extent.py`), never a filter — an excerpt is a match like any
 * other. Pure helpers, no React.
 */
import type { BadgeTone } from "./tones";

import type { Schemas } from "../api/client";
import { clock, percent } from "./format";

export type CopyExtent = Schemas["MatchOut"]["copy_extent"];
export type ExtentReason = Schemas["MatchOut"]["copy_extent_reason"];

const LABELS: Record<CopyExtent, string> = {
  full_copy: "full copy",
  excerpt: "excerpt",
  compilation_candidate: "possible compilation",
  unknown: "extent unknown",
};

const REASONS: Record<ExtentReason, string> = {
  not_classified: "not classified yet (the next matching run does it)",
  photo_collection: "photo collection: no timeline",
  no_reference_duration: "the original has no duration",
  other_originals: "other originals matched on this page",
  no_timeline: "no frame order to measure (poster or fewer than 3 frames in order)",
  weak_timeline: "frame order too inconsistent to measure",
  page_longer: "page clearly longer than the original",
  page_shorter: "page clearly shorter than the original",
  full_span: "matched frames span the original",
  narrow_span: "matched frames stay in one part of the original",
  inconclusive: "span and page duration do not decide it",
};

export function copyExtentLabel(extent: CopyExtent): string {
  return LABELS[extent];
}

/**
 * Badge tone. A full copy is what the matcher determined, so it takes the violet the design system
 * reserves for a machine decision; an excerpt or a compilation is `caution`, because a notice for
 * one would need a licence or exception review; undetermined is a fact with no state, so neutral.
 */
export function copyExtentTone(extent: CopyExtent): BadgeTone {
  if (extent === "full_copy") return "accent";
  if (extent === "excerpt" || extent === "compilation_candidate") return "caution";
  return "neutral";
}

export interface ExtentInput {
  copy_extent: CopyExtent;
  copy_extent_reason: ExtentReason;
  copy_span_s: number | null;
  copy_coverage: number | null;
  copy_other_originals: number;
}

/**
 * "excerpt — page clearly shorter than the original · spans 01:41 (3% of the original)".
 * "other originals" names how many.
 */
export function copyExtentText(match: ExtentInput): string {
  const reason =
    match.copy_extent_reason === "other_originals" && match.copy_other_originals > 0
      ? `${match.copy_other_originals} other original${match.copy_other_originals === 1 ? "" : "s"} matched on this page`
      : REASONS[match.copy_extent_reason];
  const span =
    match.copy_coverage !== null && match.copy_span_s !== null
      ? ` · spans ${clock(match.copy_span_s)} (${percent(match.copy_coverage)} of the original)`
      : "";
  return `${LABELS[match.copy_extent]} — ${reason}${span}`;
}

/** Every reason code has words; used by the tests to catch a new backend code. */
export const EXTENT_REASON_CODES = Object.keys(REASONS) as ExtentReason[];
