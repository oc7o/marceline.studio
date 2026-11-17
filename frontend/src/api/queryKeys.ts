/**
 * Staff query keys, split out of `queries.ts` (ADR 0028 performance budget, issue #82): plain
 * literals with no dependency on the API client, so `main.tsx`'s global query-cache handler (it
 * only needs `queryKeys.me` to invalidate the staff session on a 401) never pulls the staff
 * `queries.ts` — and with it `api/client.ts`'s `createClient()` — into the entry chunk that every
 * `/app` visitor downloads too. `queries.ts` re-exports this for the staff screens.
 */
export const queryKeys = {
  me: ["me"] as const,
  counts: ["counts"] as const,
  referenceSummaries: ["reference-summaries"] as const,
  referenceItems: ["reference-items"] as const,
  clusters: ["clusters"] as const,
  safety: ["safety"] as const,
  safetyIncidents: ["safety-incidents"] as const,
  safetyRelease: ["safety-release"] as const,
  safetyReportDraft: (incidentId: number) => ["safety-report-draft", incidentId] as const,
  creatorReview: ["creator-review"] as const,
  claims: (status: string) => ["claims", status] as const,
  commonFrames: (show: string) => ["common-frames", show] as const,
};
