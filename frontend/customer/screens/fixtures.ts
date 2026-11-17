/**
 * Fixtures for the screen tests, shaped exactly like the generated schema types (ADR 0036).
 *
 * The same data the Vite SPA's `src/app/testApi.tsx` builds, without its router, query client and
 * fetch stub — a server component needs none of them. Not a `.test.` file, so several test files
 * share it.
 *
 * Public ids are the API's own format (ADR 0033): `orig_` / `page_` / `inv_` and 32 hex digits. The
 * number only keeps a fixture readable; nothing parses it.
 */
import type { Host } from "@shared/app/caseFile";
import type { CustomerInvoice } from "@shared/app/invoices";
import type { Submission } from "@shared/app/submissions";

import type {
  CaseDetail,
  ClaimedAccount,
  CustomerAccount,
  CustomerFinding,
  DataRequest,
  DataRequests,
  CustomerOriginal,
  CustomerUpload,
  InviteState,
  Notice,
  NoticeFinding,
  NoticeSubmission,
  Removal,
  VerificationStatus,
} from "../lib/reads";

export const originalId = (n: number) => `orig_${String(n).padStart(32, "0")}`;
export const pageId = (n: number) => `page_${String(n).padStart(32, "0")}`;
export const invoiceId = (n: number) => `inv_${String(n).padStart(32, "0")}`;
export const noticeId = (n: number) => `notice_${String(n).padStart(32, "0")}`;

export const ORGANISATION = {
  id: `org_${"1".repeat(32)}`,
  name: "Novak Studio",
  segment: "professional",
  role: "owner",
  created_at: "2026-09-16T10:00:00Z",
} as CustomerAccount["organisation"];

export function account(overrides: Partial<CustomerAccount> = {}): CustomerAccount {
  return {
    username: "test-customer@marceline.studio",
    organisation: ORGANISATION,
    monitored_sources: "10_plus",
    can_add_originals: true,
    ...overrides,
  } as CustomerAccount;
}

export function original(overrides: Partial<CustomerOriginal> = {}): CustomerOriginal {
  return {
    id: originalId(7),
    title: "Beach day",
    kind: "video",
    duration_s: 600,
    created_at: "2026-08-01T09:00:00Z",
    state: "found_online",
    counts: { confirmed: 3, removed: 1 },
    ...overrides,
  } as CustomerOriginal;
}

/** A fully looked-up EU host; override for the other shapes. */
export function host(overrides: Partial<Host> = {}): Host {
  return {
    hostname: "video.example",
    lookup: "complete",
    looked_up_at: "2026-09-15T06:00:00Z",
    stale: false,
    addresses: ["192.0.2.10", "2001:db8::10"],
    asn: 64500,
    as_name: "EXAMPLE-HOSTING",
    provider: "Example Hosting GmbH",
    country: "DE",
    jurisdiction: "eu",
    cdn: null,
    abuse_contact: {
      email: "abuse@hosting.example",
      source: "ip_network",
      source_url: "https://rdap.db.ripe.net/ip/192.0.2.0/24",
    },
    registrar: {
      name: "Example Registrar Ltd",
      abuse_email: "abuse@registrar.example",
      source_url: "https://rdap.verisign.com/com/v1/domain/video.example",
    },
    response_history: null,
    ...overrides,
  } as Host;
}

export function finding(overrides: Partial<CustomerFinding> = {}): CustomerFinding {
  return {
    original_id: originalId(7),
    page_id: pageId(101),
    site: "video.example",
    url: "https://video.example/video/101",
    case_state: "open",
    availability: "playable",
    online: true,
    last_checked: "2026-09-14T09:00:00Z",
    confirmed_on: "2026-09-02",
    removed_at: null,
    reappeared_at: null,
    evidence: "fingerprint_match",
    extent: "full_copy",
    hosting: { page_host: host(), content_hosts: [] },
    ...overrides,
  } as CustomerFinding;
}

export function caseDetail(overrides: Partial<CaseDetail> = {}): CaseDetail {
  return {
    original: original(),
    case: {
      original_id: originalId(7),
      title: "Beach day",
      state: "open",
      opened_on: "2026-09-02",
      removed_at: null,
      reopened_on: null,
      reopen_count: 0,
      counts: { confirmed: 3, removed: 1 },
    },
    findings: [
      finding(),
      finding({
        page_id: pageId(102),
        site: "mirror.example",
        url: "https://mirror.example/v/102",
        availability: "removed",
        online: false,
        removed_at: "2026-09-10T07:00:00Z",
        extent: "excerpt",
      }),
    ],
    timeline: [
      { step: "confirmed", status: "done", at: null, on: "2026-09-02", reason: null, blocked_by_issue: null },
      {
        step: "notice_prepared",
        status: "unavailable",
        at: null,
        on: null,
        reason: "capability_not_built",
        blocked_by_issue: 33,
      },
      { step: "removed", status: "pending", at: null, on: null, reason: null, blocked_by_issue: null },
    ],
    not_available: [
      { topic: "cooperation_history", reason: "prerequisite_unavailable", blocked_by_issue: 33 },
      { topic: "notice_preparation", reason: "capability_not_built", blocked_by_issue: 33 },
      { topic: "notice_delivery", reason: "legal_review_pending", blocked_by_issue: 46 },
      { topic: "reupload_watch_coverage", reason: "capability_not_built", blocked_by_issue: null },
    ],
    ...overrides,
  } as CaseDetail;
}

export function removal(overrides: Partial<Removal> = {}): Removal {
  return {
    original_id: originalId(7),
    title: "Beach day",
    page_id: pageId(102),
    site: "mirror.example",
    url: "https://mirror.example/v/102",
    removed_at: "2026-09-10T07:00:00Z",
    confirmed_on: "2026-09-02",
    reappeared_at: null,
    last_checked: "2026-09-15T08:00:00Z",
    state: "removed",
    case_state: "removed_watching",
    handled: true,
    ...overrides,
  } as Removal;
}

export function invoice(overrides: Partial<CustomerInvoice> = {}): CustomerInvoice {
  return {
    id: invoiceId(11),
    number: "2026-00001",
    kind: "invoice",
    status: "open",
    issued_on: "2026-09-01",
    due_on: "2026-09-15",
    period_start: "2026-09-01",
    period_end: "2026-09-30",
    currency: "EUR",
    net_cents: 10000,
    vat_cents: 1900,
    gross_cents: 11900,
    vat_treatment: "domestic",
    paid_on: null,
    cancels_number: null,
    cancelled_by_number: null,
    ...overrides,
  } as CustomerInvoice;
}

export function noticeFinding(overrides: Partial<NoticeFinding> = {}): NoticeFinding {
  return {
    page_id: pageId(101),
    url: "https://video.example/watch/101",
    availability: "playable",
    extent: "full_copy",
    requires_exception_review: false,
    exception_confirmed: false,
    included: true,
    ...overrides,
  } as NoticeFinding;
}

export function notice(overrides: Partial<Notice> = {}): Notice {
  return {
    id: noticeId(1),
    original_id: originalId(7),
    status: "draft",
    is_sandbox: false,
    variant: "dsa_art16",
    language: "en",
    recipient_hostname: "video.example",
    recipient_provider_name: "Example Hosting",
    channel_kind: "abuse_email",
    recipient_email: "abuse@hosting.example",
    recipient_url: "",
    work_title: "Beach day",
    created_at: "2026-09-17T10:00:00Z",
    approved_at: null,
    marked_sent_at: null,
    responded_at: null,
    outcome: null,
    withdrawn_at: null,
    withdrawal_reason: null,
    corrected_into: null,
    findings: [noticeFinding()],
    required_confirmations: [],
    confirmations: [],
    preview_text: "preview",
    approved_text: null,
    ...overrides,
  } as Notice;
}

/** One notice write still pending, or refused after acceptance (issue #86, ADR 0046 §7). */
export function noticeSubmission(overrides: Partial<NoticeSubmission> = {}): NoticeSubmission {
  return {
    id: "sub_" + "1".repeat(32),
    kind: "notice_approve",
    status: "processing",
    code: "",
    notice_id: noticeId(1),
    received_at: "2026-09-17T09:00:00Z",
    ...overrides,
  } as NoticeSubmission;
}

/** One cloud-fingerprinting job as the customer API answers it (issue #59, ADR 0044). */
export function upload(overrides: Partial<CustomerUpload> = {}): CustomerUpload {
  return {
    id: `upl_${"1".repeat(32)}`,
    title: "New clip",
    status: "hashing",
    code: "",
    reason: null,
    original_id: null,
    declared_bytes: 1_000,
    received_bytes: 1_000,
    chunk_bytes: 100,
    next_chunk: 10,
    frames_done: 0,
    frames_total: null,
    bytes_deleted: false,
    created_at: new Date().toISOString(),
    ...overrides,
  } as CustomerUpload;
}

export function submission(overrides: Partial<Submission> = {}): Submission {
  return {
    id: "sub_1",
    title: "New clip",
    status: "processing",
    detail: null,
    created_at: "2026-09-17T09:00:00Z",
    settled_at: null,
    ...overrides,
  } as Submission;
}

/**
 * A fixture's own value, asserted present. `noUncheckedIndexedAccess` makes every index access
 * possibly-undefined and the lint rules forbid `!`, so a test that means "the first finding" says
 * so here once instead of asserting it at each use.
 */
export function must<T>(value: T | undefined, what = "fixture"): T {
  if (value === undefined) throw new Error(`${what} is missing`);
  return value;
}

/**
 * A half-finished invite redemption (issue #115, ADR 0052). There is no fixture for "expired" or
 * "revoked": the app is never told which reason a link failed for, only that it did — which is
 * why the screen takes `null` for all of them.
 */
export function inviteState(overrides: Partial<InviteState> = {}): InviteState {
  return {
    step: "password",
    email: "creator@example.invalid",
    organisation_name: "A New Studio",
    documents: [
      {
        document_id: "pilot_agreement",
        version: "2026-10-01",
        sha256: "a".repeat(64),
        url: "https://example.invalid/pilot",
      },
      {
        document_id: "service_privacy_notice",
        version: "2026-10-01",
        sha256: "b".repeat(64),
        url: "https://example.invalid/privacy",
      },
    ],
    documents_published: true,
    declaration_version: "unternehmer-2026-09-18",
    declaration_text: "Ich erkläre, dass ich Unternehmer i.S.d. § 14 BGB bin.",
    age_declaration_version: "volljaehrigkeit-2026-09-19",
    age_declaration_text: "Ich bestätige, dass ich volljährig und unbeschränkt geschäftsfähig bin.",
    declaration_language: "de",
    password_min_length: 8,
    eu_countries: ["AT", "DE", "FR"],
    outside_eu: false,
    ...overrides,
  } as InviteState;
}

/**
 * The account-control step (ADR 0055): one claimed platform account with a live code.
 *
 * The code is a made-up one of the right shape; nothing in a test may look like a real one, and
 * nothing here is a real handle.
 */
export function verificationStatus(
  overrides: Partial<VerificationStatus> = {},
): VerificationStatus {
  return {
    identity_status: "unverified",
    method: null,
    code_ttl_hours: 72,
    platforms: [
      { code: "fanhub", label: "FanHub", hosts: ["fanhub.example"], code_accepted: true },
      // ADR 0055: no platform identity check behind it, so no code is offered for it.
      { code: "own_website", label: "the creator's own website", hosts: [], code_accepted: false },
    ],
    accounts: [claimedAccount()],
    ...overrides,
  };
}

/**
 * The creator's own data requests (ADR 0056): nothing asked for yet, which is what a creator
 * opening `/app/data` for the first time sees.
 */
export function dataRequests(overrides: Partial<DataRequests> = {}): DataRequests {
  return {
    contact_email: "contact@marceline.studio",
    deletion_phrase: "DELETE",
    requests: [],
    ...overrides,
  };
}

export function dataRequest(overrides: Partial<DataRequest> = {}): DataRequest {
  return {
    kind: "export",
    status: "requested",
    requested_at: "2026-09-19T09:00:00Z",
    due_at: "2026-10-20T09:00:00Z",
    settled_at: null,
    open: true,
    ...overrides,
  };
}

export function claimedAccount(
  overrides: Partial<ClaimedAccount> = {},
): ClaimedAccount {
  return {
    id: "chal_" + "a".repeat(32),
    platform: "fanhub",
    handle: "example.creator",
    profile_url: "https://fanhub.example/example.creator",
    state: "pending",
    code: "marceline-ABCD2345",
    code_expires_at: "2026-09-22T10:00:00Z",
    method: null,
    decided_at: null,
    placed_at: null,
    ...overrides,
  };
}
