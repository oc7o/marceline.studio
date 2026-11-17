/** `matching.models.CommonFrameGroup.Kind` / `.Status` choices, words for the labeling screen
 * (issue #16). Pure and framework-free so it is testable without rendering anything. */

export const COMMON_FRAME_KINDS = [
  "unlabeled",
  "studio_intro",
  "watermark",
  "generic",
  "not_common",
] as const;
export type CommonFrameKindValue = (typeof COMMON_FRAME_KINDS)[number];

export const COMMON_FRAME_STATUSES = ["proposed", "confirmed", "rejected"] as const;
export type CommonFrameStatusValue = (typeof COMMON_FRAME_STATUSES)[number];

const KIND_LABELS: Record<CommonFrameKindValue, string> = {
  unlabeled: "Unlabeled",
  studio_intro: "Studio intro",
  watermark: "Watermark",
  generic: "Generic (e.g. a loading card)",
  not_common: "Not common (false positive)",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind as CommonFrameKindValue] ?? kind;
}

/**
 * Whether a (kind, status) pair discounts match evidence, mirrored from
 * `matching.common_frames.DISCOUNTING_STATUSES` / `label_common_group` for the confirmation copy
 * the dialog shows *before* submitting — the server has the final say, this is only a preview.
 */
export function wouldDiscount(kind: string, status: string): boolean {
  return status === "confirmed" && kind !== "not_common";
}
