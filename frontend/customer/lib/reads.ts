/**
 * Every customer read this app makes, one function each (ADR 0036).
 *
 * The paths are the customer API's (`/api/v1/app/*`, ADR 0021, 0025, 0032, 0034) and the response
 * types are the generated schema's, so `make openapi && make frontend-api` still propagates an API
 * change into a typecheck failure here. Nothing else in the app calls `djangoGet` directly: a new
 * screen adds a named read here, which is also the list a reviewer can check against Django's
 * customer surface.
 *
 * Each read is wrapped in React's `cache()`, so two components of one page that need the same
 * answer make one request — the layout's gate and the overview both want the account.
 */
import "server-only";
import { cache } from "react";

import type { Schemas } from "@shared/app/apiTypes";

import { djangoGet } from "./django";

export type CustomerAccount = Schemas["AccountOut"];
export type CustomerSession = Schemas["CustomerSessionOut"];
export type CustomerOriginal = Schemas["CustomerOriginalOut"];
export type CustomerFinding = Schemas["CustomerCaseFindingOut"];
export type CaseDetail = Schemas["CustomerCaseDetailOut"];
export type Removal = Schemas["CustomerRemovalOut"];
export type Invoice = Schemas["CustomerInvoiceOut"];
export type RightsOverview = Schemas["RightsOverviewOut"];
/** The account-control step (ADR 0055): the platforms, this organisation's claims, the live code. */
export type VerificationStatus = Schemas["VerificationStatusOut"];
export type ClaimedAccount = Schemas["ClaimedAccountOut"];
/** The creator's own export and deletion requests (ADR 0056), with the contact address. */
export type DataRequests = Schemas["DataRequestsOut"];
export type DataRequest = Schemas["DataRequestOut"];
/** The pure logic names these too (`@shared/app/submissions`); one alias, one source. */
export type Submission = Schemas["SubmissionOut"];
/** One cloud-fingerprinting job (ADR 0044); `@shared/app/uploadStatus` names it `Upload`. */
export type CustomerUpload = Schemas["UploadOut"];
export type NoticeCandidates = Schemas["NoticeCandidatesOut"];
export type Notice = Schemas["NoticeOut"];
export type NoticeFinding = Schemas["NoticeFindingOut"];
export type NoticeSubmission = Schemas["NoticeSubmissionOut"];
/** Where a half-finished invite redemption stands (issue #115, ADR 0052). */
export type InviteState = Schemas["InviteStateOut"];
/** What a working password-reset link opens onto (issue #149, ADR 0054). */
export type PasswordResetState = Schemas["PasswordResetStateOut"];
/** What a working address-verification link opens onto (issue #149, ADR 0054). Not ADR 0055's
 * `VerificationStatus`, which is about the creator's platform account: two different verifications,
 * so each name says which. */
export type EmailVerificationState = Schemas["EmailVerificationStateOut"];

/** The account and the organisation every other read is scoped to. 401 when signed out. */
export const readAccount = cache((): Promise<CustomerAccount> =>
  djangoGet<CustomerAccount>("/api/v1/app/me"),
);

/** Every original the organisation owns, newest first, with its state and counts. */
export const readOriginals = cache((): Promise<CustomerOriginal[]> =>
  djangoGet<CustomerOriginal[]>("/api/v1/app/originals"),
);

/** Every human-reviewed matching copy of those originals. Safety-flagged pages are not in it. */
export const readFindings = cache((): Promise<CustomerFinding[]> =>
  djangoGet<CustomerFinding[]>("/api/v1/app/findings"),
);

/**
 * One original's record, by its public id (`orig_…`, ADR 0033). 404 for an unknown original and
 * for another organisation's alike — the screens render both as "not found", which is all either
 * may say (ADR 0021).
 */
export const readCase = cache((originalId: string): Promise<CaseDetail> =>
  djangoGet<CaseDetail>(`/api/v1/app/cases/${encodeURIComponent(originalId)}`),
);

/** The removals log: human-reviewed copies observed gone, newest first. */
export const readRemovals = cache((): Promise<Removal[]> =>
  djangoGet<Removal[]>("/api/v1/app/removals"),
);

/** Issued invoices and cancellations, newest first (ADR 0032). Drafts are never sent. */
export const readInvoices = cache((): Promise<Invoice[]> =>
  djangoGet<Invoice[]>("/api/v1/app/invoices"),
);

/** The rights status (ADR 0034): claimant, works, licensed partners, uploaded documents. */
export const readRights = cache((): Promise<RightsOverview> =>
  djangoGet<RightsOverview>("/api/v1/app/rights"),
);

/**
 * The account-control step (ADR 0055): which platform accounts this organisation has claimed, the
 * code of an open claim, and how identity stands.
 *
 * 404 while `ACCOUNT_CONTROL_VERIFICATION_ENABLED` is off, so the page reads it through
 * `optional()` and says the step is not switched on — `me.can_verify_account` says whether asking
 * is worth it at all. On the public host the answer comes out of the signed bundle (ADR 0028).
 */
export const readVerification = cache((): Promise<VerificationStatus> =>
  djangoGet<VerificationStatus>("/api/v1/app/verification"),
);

/**
 * The creator's own data requests (ADR 0056, issue #133): what they asked for, where it stands, the
 * address it is answered from and the word a deletion request has to echo.
 *
 * 404 while `CUSTOMER_DATA_REQUESTS_ENABLED` is off, which the page renders as a sentence, and
 * `me.can_request_data` says whether asking is worth it at all. On the public host the answer comes
 * out of the signed bundle (ADR 0028) — which is the point of the feature: the export **download**
 * is development-only there, so this is the only route a creator has to either right.
 */
export const readDataRequests = cache((): Promise<DataRequests> =>
  djangoGet<DataRequests>("/api/v1/app/data-requests"),
);

/**
 * Notice drafts (issue #33, ADR 0035; on production since ADR 0046).
 *
 * The candidates are every human-reviewed copy of one original with its notice-gate blockers and
 * the suggested provider and channel per host; the drafts list carries each draft whole (its
 * preview text and its required confirmations included), so the screen needs no second read.
 */
export const readNoticeCandidates = cache((originalId: string): Promise<NoticeCandidates> =>
  djangoGet<NoticeCandidates>(`/api/v1/app/notices/candidates/${encodeURIComponent(originalId)}`),
);

export const readNotices = cache((): Promise<Notice[]> => djangoGet<Notice[]>("/api/v1/app/notices"));

/**
 * Notice writes the public host accepted but has not applied yet, and what became of them
 * (ADR 0046). Empty on development, where a write is applied while the customer waits.
 *
 * No page reads it yet — the words a block for it needs do not fit `/app`'s first-load budget
 * while the SPA still ships them (ADR 0028 d.1). Named here because this file is the list of
 * customer endpoints a reviewer checks against Django's surface.
 */
export const readNoticeSubmissions = cache((): Promise<NoticeSubmission[]> =>
  djangoGet<NoticeSubmission[]>("/api/v1/app/notices/submissions"),
);

/** The organisation's own uploads that are being added or were not added (ADR 0028 write path). */
export const readSubmissions = cache((): Promise<Submission[]> =>
  djangoGet<Submission[]>("/api/v1/app/fingerprints/submissions"),
);

/**
 * The files this organisation sent to be fingerprinted (issue #59, ADR 0044), newest first.
 *
 * Development only, like the rights vault: on the public host the whole route is 404, so a page
 * reads it through `optional()` and simply shows nothing. `me.can_upload_media` says whether
 * asking is worth it at all.
 */
export const readUploads = cache((): Promise<CustomerUpload[]> =>
  djangoGet<CustomerUpload[]>("/api/v1/app/uploads"),
);

/**
 * Where an invite redemption stands, and everything its current step needs (ADR 0052).
 *
 * The **one read this app makes without a session** — whoever opens the link has no account yet.
 * Django answers one identical 404 for unknown, expired, withdrawn, already-used and "the feature
 * is off", so the caller may only ever render one page for all of them: never branch on the
 * reason, because there is none to be had.
 */
export const readInvite = cache((token: string): Promise<InviteState> =>
  djangoGet<InviteState>(`/api/v1/app/invite/${encodeURIComponent(token)}`),
);

/**
 * Whether a mailed password-reset link still works, and the minimum length to say (ADR 0054).
 *
 * Read **without a session**, like the invite's: whoever opens the link cannot sign in, that being
 * the point. Django answers one identical 404 for unknown, malformed, expired, already used and "the
 * feature is off", so the page renders one screen for all of them and never branches on the reason.
 */
export const readPasswordReset = cache((token: string): Promise<PasswordResetState> =>
  djangoGet<PasswordResetState>(
    `/api/v1/app/auth/password-reset/${encodeURIComponent(token)}`,
  ),
);

/**
 * Whether a mailed verification link still works (ADR 0054). Also session-less, same one 404.
 *
 * Reading it **confirms nothing**: the act is the POST the page's button sends, because mail
 * scanners and link previews fetch a link on their own and a confirmation a scanner made is nobody's.
 */
export const readEmailVerification = cache((token: string): Promise<EmailVerificationState> =>
  djangoGet<EmailVerificationState>(
    `/api/v1/app/auth/verify-email/${encodeURIComponent(token)}`,
  ),
);
