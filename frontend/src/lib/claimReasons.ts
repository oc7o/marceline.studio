/**
 * Staff-only reason labels for refused upload claims (issue #29 amendment, operator 2026-09-17,
 * `ClaimsScreen`). The customer never sees these — only the neutral 409 `claim_needs_review`.
 */
export const CLAIM_REASON_LABELS: Record<string, string> = {
  owned_by_other: "Same file, owned by another organisation",
  unowned_existing: "Same file, registered but owned by nobody",
  hash_overlap: "Hashes largely duplicate another original",
};

export function claimReasonLabel(reason: string): string {
  return CLAIM_REASON_LABELS[reason] ?? reason;
}
