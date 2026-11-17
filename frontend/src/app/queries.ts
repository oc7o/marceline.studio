/**
 * Customer API hooks (`/api/v1/app/…`, ADR 0021 and ADR 0023).
 *
 * Kept apart from `src/api/queries.ts` on purpose: that file speaks the staff review API. The two
 * audiences share a client and a session mechanism, never a queryset and never a query key.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, type Schemas, api, unwrap } from "../api/client";
import type { paths } from "../api/schema";
import {
  type AddOriginalAccepted,
  type AddOriginalAdded,
  type Submission,
  anyProcessing,
  settledSince,
} from "./submissions";

// The response shapes live in `apiTypes.ts` (types only, no dependency on this module's
// TanStack Query or fetch client) and are re-exported here, so every existing import is unchanged.
export type {
  CaseDetail,
  CaseStep,
  CaseUnknown,
  CustomerAccount,
  CustomerFinding,
  CustomerOriginal,
  CustomerSession,
  Notice,
  NoticeCandidateFinding,
  NoticeCandidates,
  NoticeChannel,
  NoticeConfirmation,
  NoticeFinding,
  NoticeProvider,
  Organisation,
  Removal,
  RequiredConfirmation,
} from "./apiTypes";
import type {
  AddOriginalRequest,
  ClientEventRequest,
  CustomerAccount,
  CustomerSession,
  Notice,
} from "./apiTypes";

/**
 * Every customer key starts with `"app"`, so signing in or out can drop the lot in one call
 * without touching the staff SPA's cache. Keep new customer keys under that prefix.
 */
export const customerKeys = {
  session: ["app", "session"] as const,
  me: ["app", "me"] as const,
  originals: ["app", "originals"] as const,
  findings: ["app", "findings"] as const,
  case: (originalId: string) => ["app", "case", originalId] as const,
  removals: ["app", "removals"] as const,
  invoices: ["app", "invoices"] as const,
  rights: ["app", "rights"] as const,
  noticeCandidates: (originalId: string) => ["app", "notices", "candidates", originalId] as const,
  notices: ["app", "notices"] as const,
  notice: (noticeId: string) => ["app", "notices", noticeId] as const,
  submissions: ["app", "submissions"] as const,
  noticeSubmissions: ["app", "notices", "submissions"] as const,
};

/**
 * A 4xx is an answer — 404 means "not found", 401 "signed out" — so it is never retried. Only a
 * network failure or a 5xx is worth one more attempt.
 */
export function retryUnlessAnswered(failureCount: number, error: Error): boolean {
  return !(error instanceof ApiError && error.status < 500) && failureCount < 2;
}

/** Every original the organisation owns, newest first, with its state and counts. */
export function useCustomerOriginals() {
  return useQuery({
    queryKey: customerKeys.originals,
    queryFn: () => unwrap(api.GET("/api/v1/app/originals")),
    retry: retryUnlessAnswered,
  });
}

/** Every page the organisation's originals were found on. Safety-flagged pages are not in it. */
export function useCustomerFindings() {
  return useQuery({
    queryKey: customerKeys.findings,
    queryFn: () => unwrap(api.GET("/api/v1/app/findings")),
    retry: retryUnlessAnswered,
  });
}

/**
 * One original's case file, by the original's public id (`orig_…`, ADR 0033). 404 for an unknown
 * original and for someone else's alike — the screens render both as "not found", which is all
 * either of them may say.
 */
export function useCustomerCase(originalId: string) {
  return useQuery({
    queryKey: customerKeys.case(originalId),
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/app/cases/{original_id}", {
          params: { path: { original_id: originalId } },
        }),
      ),
    retry: retryUnlessAnswered,
  });
}

/** The removals log: confirmed findings observed gone, newest first. */
export function useCustomerRemovals() {
  return useQuery({
    queryKey: customerKeys.removals,
    queryFn: () => unwrap(api.GET("/api/v1/app/removals")),
    retry: retryUnlessAnswered,
  });
}

/** The organisation's issued invoices and cancellations, newest first. Drafts are never sent. */
export function useCustomerInvoices() {
  return useQuery({
    queryKey: customerKeys.invoices,
    queryFn: () => unwrap(api.GET("/api/v1/app/invoices")),
    retry: retryUnlessAnswered,
  });
}

/** The rights status (ADR 0034): claimant, works, licensed partners, uploaded documents. */
export function useCustomerRights() {
  return useQuery({
    queryKey: customerKeys.rights,
    queryFn: () => unwrap(api.GET("/api/v1/app/rights")),
    retry: retryUnlessAnswered,
  });
}

type RightsOverviewBody = Schemas["RightsOverviewOut"];
type RightsWriteAccepted = Schemas["RightsWriteAcceptedOut"];

/**
 * On development a rights write answers with the whole overview, so a successful mutation simply
 * replaces the cached one. On production (ADR 0028 write path) it answers 202
 * `RightsWriteAcceptedOut` instead: accepted into the outbox, not applied yet — nothing to cache,
 * the mutation's own `data` carries the `pending` flag the screen renders
 * (`RightsScreen.WritePending`, `rights.isPendingWrite`). Never retried: a declaration or an
 * upload must not be sent twice by accident.
 */
function useRightsWrite<Variables>(
  send: (variables: Variables) => Promise<RightsOverviewBody | RightsWriteAccepted>,
) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: send,
    onSuccess: (result) => {
      if ("pending" in result) return;
      queryClient.setQueryData(customerKeys.rights, result);
    },
  });
}

export function useSetClaimantType() {
  return useRightsWrite((claimantType: Schemas["ClaimantTypeIn"]["claimant_type"]) =>
    unwrap(api.PUT("/api/v1/app/rights/claimant", { body: { claimant_type: claimantType } })),
  );
}

export function useRequestAccountToken() {
  return useRightsWrite(() => unwrap(api.POST("/api/v1/app/rights/claimant/token")));
}

export function useDeclareWork() {
  return useRightsWrite(
    ({ originalId, body }: { originalId: string; body: Schemas["WorkDeclarationIn"] }) =>
      unwrap(
        api.POST("/api/v1/app/rights/works/{original_id}/declaration", {
          params: { path: { original_id: originalId } },
          body,
        }),
      ),
  );
}

export function useWithdrawDeclaration() {
  return useRightsWrite(
    ({ originalId, body }: { originalId: string; body: Schemas["WithdrawalIn"] }) =>
      unwrap(
        api.POST("/api/v1/app/rights/works/{original_id}/withdrawal", {
          params: { path: { original_id: originalId } },
          body,
        }),
      ),
  );
}

export function useDeclareAuthority() {
  return useRightsWrite(
    ({ originalId, body }: { originalId: string; body: Schemas["AuthorityIn"] }) =>
      unwrap(
        api.POST("/api/v1/app/rights/works/{original_id}/authority", {
          params: { path: { original_id: originalId } },
          body,
        }),
      ),
  );
}

export function useWithdrawAuthority() {
  return useRightsWrite(
    ({ originalId, body }: { originalId: string; body: Schemas["WithdrawalIn"] }) =>
      unwrap(
        api.POST("/api/v1/app/rights/works/{original_id}/authority/withdrawal", {
          params: { path: { original_id: originalId } },
          body,
        }),
      ),
  );
}

export function useAddPartner() {
  return useRightsWrite((body: Schemas["PartnerIn"]) =>
    unwrap(api.POST("/api/v1/app/rights/partners", { body })),
  );
}

export function useRemovePartner() {
  return useRightsWrite((partnerId: string) =>
    unwrap(
      api.DELETE("/api/v1/app/rights/partners/{partner_id}", {
        params: { path: { partner_id: partnerId } },
      }),
    ),
  );
}

export function useAffirmNoPartners() {
  return useRightsWrite((body: Schemas["NoPartnersIn"]) =>
    unwrap(api.POST("/api/v1/app/rights/partners/none", { body })),
  );
}

type ProofUploadBody = NonNullable<
  paths["/api/v1/app/rights/proofs"]["post"]["requestBody"]
>["content"]["multipart/form-data"];

/** Multipart upload. openapi-fetch passes a `FormData` body through and lets the browser set the
 * boundary; the CSRF header comes from the client middleware like every other write. */
export function useUploadProof() {
  return useRightsWrite((form: FormData) =>
    unwrap(
      api.POST("/api/v1/app/rights/proofs", { body: form as unknown as ProofUploadBody }),
    ),
  );
}

/**
 * Notice drafts (issue #33, ADR 0035): "Prepare notice draft" per record and provider.
 * Development only for now, like rights and invoices.
 */

/** Every confirmed finding of this record, with its notice-gate blockers and suggested provider
 * and channel per host — what the draft-creation form offers. */
export function useNoticeCandidates(originalId: string) {
  return useQuery({
    queryKey: customerKeys.noticeCandidates(originalId),
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/app/notices/candidates/{original_id}", {
          params: { path: { original_id: originalId } },
        }),
      ),
    retry: retryUnlessAnswered,
  });
}

/** Every notice draft of this organisation, newest first. */
export function useNotices() {
  return useQuery({
    queryKey: customerKeys.notices,
    queryFn: () => unwrap(api.GET("/api/v1/app/notices")),
    retry: retryUnlessAnswered,
  });
}

/**
 * Notice writes the public host accepted but has not applied yet, and what became of them
 * (ADR 0046). Empty on development, where a write is applied while the customer waits.
 *
 * No screen reads this yet: `/app`'s first load is at the 150 KB phase 1 ceiling (ADR 0028 d.1)
 * and a block for it needs words, which ship in that first load. The write's own 202 already
 * shows the customer a pending line; this is for the screen that follows the SPA's retirement.
 */
export function useNoticeSubmissions() {
  return useQuery({
    queryKey: customerKeys.noticeSubmissions,
    queryFn: () => unwrap(api.GET("/api/v1/app/notices/submissions")),
    // A write is applied within about a minute; a customer watching the screen should see it
    // settle without reloading, and the answer is a handful of rows.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((row) => row.status === "processing") ? 15_000 : false,
    retry: retryUnlessAnswered,
  });
}

/** One notice draft, by its public id (`notice_…`). */
export function useNotice(noticeId: string) {
  return useQuery({
    queryKey: customerKeys.notice(noticeId),
    queryFn: () =>
      unwrap(api.GET("/api/v1/app/notices/{notice_id}", { params: { path: { notice_id: noticeId } } })),
    retry: retryUnlessAnswered,
  });
}

/** Every notice write answers with the affected draft; cache both its own key and the list, and
 * the candidates for the record it belongs to so a used-up finding drops off the offer.
 *
 * The list invalidation is `exact: true` on purpose: `customerKeys.notices` (`["app","notices"]`)
 * is a *prefix* of every detail key (`["app","notices",noticeId]`), so a non-exact invalidation
 * would also mark the detail query just set above stale and trigger a refetch racing that
 * update — the write's own response is already the freshest copy of that one draft. */
/** What a notice write answers: the draft on development, or "accepted" on production (ADR 0046). */
type NoticeWriteResult = Notice | Schemas["NoticeWriteAcceptedOut"];

function useNoticeWrite<Variables>(
  originalId: string,
  send: (variables: Variables) => Promise<NoticeWriteResult>,
) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: send,
    onSuccess: (result) => {
      // On production the write was only accepted into the outbox: there is no draft to cache yet,
      // and caching the acceptance under an undefined id would poison the detail query. The lists
      // are refetched either way, so the applied write appears once development has applied it.
      // Narrowed the way `useRightsWrite` does it, on the field itself.
      if (!("pending" in result)) {
        queryClient.setQueryData(customerKeys.notice(result.id), result);
      }
      void queryClient.invalidateQueries({ queryKey: customerKeys.notices, exact: true });
      void queryClient.invalidateQueries({ queryKey: customerKeys.noticeCandidates(originalId) });
    },
  });
}

export function useCreateNotice(originalId: string) {
  return useNoticeWrite(originalId, (body: Schemas["CreateNoticeIn"]) =>
    unwrap(api.POST("/api/v1/app/notices", { body })),
  );
}

export function useApproveNotice(originalId: string) {
  return useNoticeWrite(
    originalId,
    ({ noticeId, body }: { noticeId: string; body: Schemas["ConfirmationIn"][] }) =>
      unwrap(
        api.POST("/api/v1/app/notices/{notice_id}/approve", {
          params: { path: { notice_id: noticeId } },
          body,
        }),
      ),
  );
}

export function useMarkNoticeSent(originalId: string) {
  return useNoticeWrite(originalId, (noticeId: string) =>
    unwrap(api.POST("/api/v1/app/notices/{notice_id}/mark-sent", { params: { path: { notice_id: noticeId } } })),
  );
}

export function useSandboxSendNotice(originalId: string) {
  return useNoticeWrite(
    originalId,
    ({ noticeId, body }: { noticeId: string; body: Schemas["SandboxSendIn"] }) =>
      unwrap(
        // `sandbox-send` before the id: it is the one path the public host must refuse, and the
        // role allowlist is a prefix allowlist (ADR 0046).
        api.POST("/api/v1/app/notices/sandbox-send/{notice_id}", {
          params: { path: { notice_id: noticeId } },
          body,
        }),
      ),
  );
}

export function useRecordNoticeResponse(originalId: string) {
  return useNoticeWrite(
    originalId,
    ({ noticeId, body }: { noticeId: string; body: Schemas["ResponseIn"] }) =>
      unwrap(
        api.POST("/api/v1/app/notices/{notice_id}/response", {
          params: { path: { notice_id: noticeId } },
          body,
        }),
      ),
  );
}

export function useWithdrawNotice(originalId: string) {
  return useNoticeWrite(
    originalId,
    ({ noticeId, body }: { noticeId: string; body: Schemas["WithdrawIn"] }) =>
      unwrap(
        api.POST("/api/v1/app/notices/{notice_id}/withdraw", {
          params: { path: { notice_id: noticeId } },
          body,
        }),
      ),
  );
}

export function useCorrectNotice(originalId: string) {
  return useNoticeWrite(
    originalId,
    ({ noticeId, body }: { noticeId: string; body: Schemas["CorrectIn"] }) =>
      unwrap(
        api.POST("/api/v1/app/notices/{notice_id}/correct", {
          params: { path: { notice_id: noticeId } },
          body,
        }),
      ),
  );
}

/**
 * Is this browser signed in? Answers 200 either way — it is the one customer endpoint that needs
 * no session — and it sets the `csrftoken` cookie the login POST has to echo.
 */
export function useCustomerSession() {
  return useQuery({
    queryKey: customerKeys.session,
    queryFn: () => unwrap(api.GET("/api/v1/app/auth/session")),
    staleTime: 5 * 60_000,
  });
}

/**
 * The account and the organisation every other customer request is scoped to. Every screen calls
 * this from behind the session gate, so it needs no `enabled` flag there; `CustomerShell` renders
 * outside the gate too (the sign-in page), and passes `enabled: false` while signed out so this
 * never fires against an anonymous session.
 */
export function useCustomerAccount(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: customerKeys.me,
    queryFn: () => unwrap(api.GET("/api/v1/app/me")),
    staleTime: 5 * 60_000,
    enabled: options?.enabled ?? true,
  });
}

export function useCustomerLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    // Never retried. A 429 must not become a retry loop, and a wrong password must not be sent
    // twice: five failures lock the account for fifteen minutes (ADR 0023).
    retry: false,
    mutationFn: async (credentials: { username: string; password: string }): Promise<CustomerAccount> => {
      // The session GET first, for the CSRF cookie: nothing else sets it, and the POST is refused
      // without it. The `X-CSRFToken` header is added by the client's middleware.
      await unwrap(api.GET("/api/v1/app/auth/session"));
      return unwrap(api.POST("/api/v1/app/auth/login", { body: credentials }));
    },
    onSuccess: (account) => {
      // Drop whatever a previous session cached, then seed what this response just told us.
      // Prefix removal rather than `queryClient.clear()`: clear() leaves mounted observers holding
      // their last result — the shell, the gate and the screen all read the session — and it would
      // also throw away the staff SPA's cache.
      queryClient.removeQueries({ queryKey: ["app"] });
      queryClient.setQueryData(customerKeys.me, account);
      queryClient.setQueryData<CustomerSession>(customerKeys.session, {
        authenticated: true,
        username: account.username,
      });
    },
  });
}

export type { AddOriginalRequest, ClientEventRequest } from "./apiTypes";
/** 201/200: the original itself; 202 (production): a submission that is being added. */
export type AddOriginalResult = AddOriginalAdded | AddOriginalAccepted;

/**
 * Register an original from fingerprints computed in this browser (`POST /app/fingerprints`).
 * Never retried: a refusal is an answer, and a 429 must not turn into a loop. The originals,
 * findings and submissions are refetched afterwards, so the overview shows the new original — or,
 * on production, that it is being added.
 */
export function useAddOriginal() {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (body: AddOriginalRequest): Promise<AddOriginalResult> =>
      unwrap(api.POST("/api/v1/app/fingerprints", { body })),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: customerKeys.originals });
      void queryClient.invalidateQueries({ queryKey: customerKeys.findings });
      void queryClient.invalidateQueries({ queryKey: customerKeys.submissions });
    },
  });
}

/** How often the overview asks again while an upload is being added (production). */
export const SUBMISSION_POLL_MS = 15_000;

/**
 * Uploads the organisation sent that are being added or were not added (`GET
 * /app/fingerprints/submissions`). Asked again every `SUBMISSION_POLL_MS` only while one is being
 * added; when one settles, the originals and findings are refetched so the new original appears.
 */
export function useSubmissions(enabled: boolean) {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: customerKeys.submissions,
    enabled,
    retry: retryUnlessAnswered,
    queryFn: async (): Promise<Submission[]> => {
      const before = queryClient.getQueryData<Submission[]>(customerKeys.submissions);
      const after = await unwrap(api.GET("/api/v1/app/fingerprints/submissions"));
      if (settledSince(before, after)) {
        void queryClient.invalidateQueries({ queryKey: customerKeys.originals });
        void queryClient.invalidateQueries({ queryKey: customerKeys.findings });
      }
      return after;
    },
    refetchInterval: (query) => (anyProcessing(query.state.data) ? SUBMISSION_POLL_MS : false),
  });
}

/**
 * Count a local run that produced no fingerprints (`codec_unsupported` / `client_failed`). Fire
 * and forget: the customer has already been told what went wrong, and a failed count is not news.
 */
export function useRecordClientEvent() {
  return useMutation({
    retry: false,
    mutationFn: (body: ClientEventRequest) =>
      unwrap(api.POST("/api/v1/app/fingerprints/client-events", { body })),
  });
}

export function useCustomerLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: () => unwrap(api.POST("/api/v1/app/auth/logout")),
    onSuccess: (session) => {
      // The session first, so every observer flips to "signed out" and the gate navigates away;
      // then drop the account data. A signed-out customer must not be left looking at their own
      // organisation, which is exactly what `clear()` did — a removed query leaves its mounted
      // observers rendering the result they already had.
      queryClient.setQueryData(customerKeys.session, session);
      queryClient.removeQueries({ queryKey: customerKeys.me });
    },
  });
}
