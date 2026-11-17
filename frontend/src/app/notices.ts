/**
 * Pure helpers for the notice-draft screen (`/app/originals/$originalId/notices`, issue #33,
 * ADR 0035). The API sends codes; the words live in `src/i18n` under `notices.*`, except the
 * confirmation and rendered notice texts, which the server owns and the screen shows verbatim —
 * what the customer confirms and approves must be exactly what is stored and sent.
 *
 * Wording (frontend/src/app/CLAUDE.md): Marceline states technical facts, the customer makes the legal
 * assertion. "Prepare notice draft", never "send" — the customer reviews, signs and sends this
 * draft from their own mailbox or provider account (v1 is customer-sent drafts only).
 */
import { ApiError } from "../api/errors";
import type { Schemas } from "./apiTypes";
import type { Notice, NoticeCandidateFinding, NoticeChannel } from "./apiTypes";

export type NoticeSubmission = Schemas["NoticeSubmissionOut"];
export type NoticeVariant = Schemas["CreateNoticeIn"]["variant"];
export type NoticeLanguage = Schemas["CreateNoticeIn"]["language"];
export type NoticeStatus = Notice["status"];
export type ChannelKind = Schemas["RecipientIn"]["channel_kind"];
export type NoticeOutcome = NonNullable<Notice["outcome"]>;
export type ConfirmationKind = Schemas["ConfirmationIn"]["kind"];
export type FindingBlocker = NoticeCandidateFinding["blockers"][number];

export const VARIANTS: NoticeVariant[] = ["dsa_art16", "dmca_512c", "dmca_512d", "provider_policy"];
export const CHANNEL_KINDS: ChannelKind[] = [
  "dsa_art16",
  "dmca_512c",
  "dmca_512d",
  "provider_copyright_policy",
  "provider_copyright_policy_dmca_style",
  "abuse_email",
  "web_form",
  "forwarding_only",
  "none_known",
];
export const OUTCOMES: NoticeOutcome[] = ["removed", "refused", "no_response"];
export const CONFIRMATION_KINDS: ConfirmationKind[] = [
  "rights",
  "identity",
  "authorisation",
  "exceptions",
  "recipient",
  "accuracy",
  "forwarding_warning",
];
export const WITHDRAWAL_REASONS = [
  "mistaken_match",
  "rights_changed",
  "already_removed",
  "customer_declined",
  "other",
] as const;
export type WithdrawalReason = (typeof WITHDRAWAL_REASONS)[number];
export const CORRECTION_REASONS = ["wrong_recipient", "wrong_findings", "factual_error", "other"] as const;
export type CorrectionReason = (typeof CORRECTION_REASONS)[number];

/** Every blocker the notice gate (ADR 0034 rights + ADR 0035 notice) can name, for the words test. */
export const BLOCKERS: FindingBlocker[] = [
  // Rights side (ADR 0034): the same codes `rights.ts` lists.
  "work_not_owned",
  "claimant_type_missing",
  "identity_not_verified",
  "rights_record_missing",
  "documents_not_complete",
  "authority_not_declared",
  "declaration_incomplete",
  "rights_disputed",
  "rights_revoked",
  "rights_expired",
  "competing_claim",
  "underlying_owner_missing",
  "mandate_missing",
  "mandate_not_yet_valid",
  "mandate_expired",
  "mandate_revoked",
  "mandate_notice_clause_not_recorded",
  "schedule_expired",
  "partners_not_affirmed",
  // This notice's own side (ADR 0035).
  "no_high_confirmed_match",
  "page_not_available",
  "availability_not_recent",
  "licensed_partner_match",
  "exception_review_required",
  "no_supported_channel",
];

export const STATUSES: NoticeStatus[] = [
  "draft",
  "customer_approved",
  "marked_sent_by_customer",
  "provider_responded",
  "withdrawn",
  "corrected",
];

export const STATUS_TONES: Record<NoticeStatus, string> = {
  draft: "bg-slate-100 text-slate-700",
  customer_approved: "bg-sky-100 text-sky-900",
  marked_sent_by_customer: "bg-amber-100 text-amber-900",
  provider_responded: "bg-emerald-100 text-emerald-900",
  withdrawn: "bg-rose-100 text-rose-900",
  corrected: "bg-slate-100 text-slate-700",
};

/**
 * The files a customer can take away once a draft is approved (ADR 0035 amendment 2026-09-18).
 *
 * `txt` is the approved text byte for byte — what a provider's web form is filled with by
 * pasting. `eml` is an unsent message their own mail program opens, for an email channel; a
 * `mailto:` link cannot carry a notice that lists several URLs. `annex` is the evidence annex,
 * for a form's "additional information" field and for the customer's own file.
 */
export const EXPORT_FORMATS = ["txt", "eml", "annex"] as const;
export type ExportFormat = (typeof EXPORT_FORMATS)[number];

/** Refusal codes the notice endpoints answer with (409). Each has words. */
export const ERROR_CODES = [
  "unknown_variant",
  "unknown_language",
  "no_findings_selected",
  "unknown_page",
  "finding_not_eligible",
  "not_draft",
  "no_findings_included",
  "confirmation_incomplete",
  "finding_no_longer_eligible",
  "sandbox_cannot_mark_real_send",
  "not_approved",
  "not_sandbox",
  "recipient_not_allowlisted",
  "unknown_outcome",
  "not_sent",
  "unknown_reason",
  "already_closed",
] as const;
export type ErrorCode = (typeof ERROR_CODES)[number];

/**
 * Refusals the public host answers with before a write is even accepted (ADR 0046): the outbox
 * switched off, development not pulling, or too many writes in an hour. They share one sentence,
 * because the only thing that differs is our plumbing and the only thing the customer needs is the
 * same either way — nothing was saved, try again shortly. "Something went wrong" is what this
 * avoids; three near-identical sentences would cost `/app`'s first-load budget (ADR 0028 d.1) for
 * a distinction the customer cannot act on.
 */
export const UNAVAILABLE_CODES = [
  "notice_write_unavailable",
  "processing_paused",
  "too_many_requests",
] as const;
const NOT_SAVED_KEY = "notices.errors.codes.not_saved";

/**
 * Codes only development can answer with, once it applies a write the public host accepted
 * (ADR 0046 §7, issue #86). They never reach a customer as an HTTP error — they arrive later, in
 * `GET /app/notices/submissions`, which is the only place a customer learns that the step they
 * asked for was refused after all.
 *
 * `notice_not_found` is the draft itself going out of reach (its page became safety-flagged in
 * between); `finding_rights_blocked` and `finding_unavailable` are how the backend narrows
 * `finding_not_eligible` / `finding_no_longer_eligible` when every blocker of every page in the
 * refusal was on one side only (`customer_writes.py _submission_code_for`) — the generic code by
 * itself says only "something changed", which is true but useless. The handlers' other internal
 * codes (`invalid_payload`, `no_organisation`) still have no words: they are never the result of
 * anything a customer did, so the generic line is the honest answer for them.
 */
export const SUBMISSION_CODES = [
  "notice_not_found",
  "finding_rights_blocked",
  "finding_unavailable",
] as const;

/** Every refusal that has words of its own, in any of the three families. */
const WORDED_CODES: readonly string[] = [
  ...ERROR_CODES,
  ...UNAVAILABLE_CODES,
  ...SUBMISSION_CODES,
];

/** The i18n key for an outcome code, or null when it has no words of its own. */
export function codeKey(code: string): string | null {
  if ((UNAVAILABLE_CODES as readonly string[]).includes(code)) return NOT_SAVED_KEY;
  return WORDED_CODES.includes(code) ? `notices.errors.codes.${code}` : null;
}

/** The i18n key for a failed write: the refusal's own words when it has any, else a generic line. */
export function errorKey(error: unknown): string {
  if (error instanceof ApiError && error.status !== 404) {
    return codeKey(error.message) ?? "notices.errors.generic";
  }
  return "notices.errors.generic";
}

/**
 * A write answered 202 (production, ADR 0028 write path, ADR 0046): the public host accepted it
 * into the outbox and development applies it shortly. Nothing has happened yet — the screen says
 * so and refetches; it never shows the step as done.
 */
export function isPendingWrite(result: unknown): result is { pending: boolean } {
  return Boolean(result) && typeof result === "object" && "pending" in (result as object);
}

/**
 * Which submissions the block on `/app/notices` and `/app/originals/{id}/notices` shows: writes
 * still being applied, and ones that were refused (issue #86, ADR 0046 §7). A write that went
 * through needs no line — the draft itself, open on this same list, is the record of success —
 * so as soon as a submission's status flips to `done` the next read of the page simply omits it;
 * there is no separate fade-out and nothing to delete.
 */
export function isUnsettled(submission: { status: string }): boolean {
  return submission.status !== "done";
}

/** Which notice writes `GET /app/notices/submissions` reports on (`notices.py SUBMITTED_KINDS`);
 * a download is deliberately not one of them — nothing is waiting for the customer there. */
export const NOTICE_WRITE_KINDS = [
  "notice_create",
  "notice_approve",
  "notice_mark_sent",
  "notice_response",
  "notice_withdraw",
  "notice_correct",
] as const;

/** The i18n key naming what a submission's kind was ("Prepare notice draft", "Approve", …). */
export function submissionKindKey(kind: string): string {
  return (NOTICE_WRITE_KINDS as readonly string[]).includes(kind)
    ? `notices.submissions.kinds.${kind}`
    : "notices.submissions.kinds.other";
}

/**
 * The sentence under one submission in the block: a translation key, never server text.
 * `processing` and `failed` have one line each; `refused` reuses the very sentences the live
 * create/approve screen shows for the same codes (`codeKey`), falling back to a neutral line with
 * no code in it for anything without words of its own.
 */
export function submissionMessage(submission: Pick<NoticeSubmission, "status" | "code">): string {
  if (submission.status === "processing") return "notices.submissions.processing";
  if (submission.status === "failed") return "notices.submissions.failed";
  return codeKey(submission.code) ?? "notices.submissions.refusedOther";
}

/**
 * Why a finding cannot be selected yet, as one line of prose.
 *
 * Every `notices.blockers.*` string is a whole sentence ending in a full stop, so the sentences are
 * separated by a space and by nothing else. They used to be joined with `"; "`, which printed
 * ".; " between them on the screen; the rights screen renders the very same codes and has always
 * shown them as plain sentences, one per list item.
 *
 * `word` is the caller's own lookup, so the template-literal `t()` call stays in the screen, where
 * `translationKeys.test.ts`'s slice guard can see it.
 */
export function blockerLine(
  blockers: readonly FindingBlocker[],
  word: (code: FindingBlocker) => string,
): string {
  return blockers.map(word).join(" ");
}

/** Only a finding with no blockers at all may be selected into a draft. */
export function isEligible(finding: NoticeCandidateFinding): boolean {
  return finding.blockers.length === 0;
}

/** A finding blocked on nothing but the per-finding exception/fair-use tick: selectable once the
 * customer ticks it, unlike every other blocker (required whenever a copy is not a
 * current full copy, in addition to the batch exceptions confirmation). */
export function onlyNeedsExceptionTick(finding: NoticeCandidateFinding): boolean {
  return finding.blockers.length === 1 && finding.blockers[0] === "exception_review_required";
}

export function isSelectable(finding: NoticeCandidateFinding, exceptionTicked: ReadonlySet<string>): boolean {
  if (isEligible(finding)) return true;
  return onlyNeedsExceptionTick(finding) && exceptionTicked.has(finding.page_id);
}

/** The declaration/notice language to use for the server's texts, from the UI language. */
export function declarationLanguage(language: string): NoticeLanguage {
  return language.startsWith("de") ? "de" : "en";
}

/** A channel usable at all in v1 — `not_supported` and `needs_classification` are not offered. */
export function isUsableChannel(channel: NoticeChannel): boolean {
  return channel.support === "primary" || channel.support === "delisting_draft";
}

/** The variant a channel kind renders as — the customer still confirms the exact recipient. */
export function variantForChannelKind(kind: ChannelKind): NoticeVariant {
  if (kind === "dsa_art16") return "dsa_art16";
  if (kind === "dmca_512c") return "dmca_512c";
  if (kind === "dmca_512d") return "dmca_512d";
  return "provider_policy";
}

/** Which statuses still allow a withdrawal or a correction — anything not already closed. */
export function isOpen(status: NoticeStatus): boolean {
  return status !== "withdrawn" && status !== "corrected";
}

export function canApprove(notice: Notice): boolean {
  return notice.status === "draft";
}

export function canMarkSent(notice: Notice): boolean {
  return notice.status === "customer_approved" && !notice.is_sandbox;
}

export function canSandboxSend(notice: Notice): boolean {
  return notice.status === "customer_approved" && notice.is_sandbox;
}

export function canRecordResponse(notice: Notice): boolean {
  return notice.status === "marked_sent_by_customer";
}

/**
 * Whether the draft has a file to hand over: it must be approved (the text is frozen only then)
 * and still open. A withdrawn or replaced draft keeps its text on the screen as the customer's
 * own record, but it is not a notice to take away, and the server refuses it too.
 */
export function canExport(notice: Notice): boolean {
  return Boolean(notice.approved_text) && isOpen(notice.status);
}

/** Where the SPA points a download link: the customer API itself, same as the invoice PDF. */
export function noticeExportPath(id: string, format: ExportFormat): string {
  return `/api/v1/app/notices/${encodeURIComponent(id)}/export/${format}`;
}

/** A `mailto:` link for an email channel, prefilled with the approved text — the customer's own
 * mail program does the sending; Marceline never transmits it. Null for a web-form channel: the
 * screen shows the fields to paste there instead. */
export function mailtoLink(notice: Notice): string | null {
  if (!notice.recipient_email || !notice.approved_text) return null;
  const subject = encodeURIComponent(
    `${notice.work_title} — ${notice.recipient_provider_name || notice.recipient_hostname}`,
  );
  const body = encodeURIComponent(notice.approved_text);
  return `mailto:${notice.recipient_email}?subject=${subject}&body=${body}`;
}

/**
 * How many matching copies this notice currently names: findings the customer has not dropped
 * before approving it (`included`). A later correction narrows this without changing the count of
 * `findings` itself, which keeps every row this draft ever reported.
 */
export function includedFindingsCount(notice: Notice): number {
  return notice.findings.filter((finding) => finding.included).length;
}

/** The account-wide list's status tone, as a meaning rather than a colour (ADR 0036: each app
 * renders its own tokens for it). Neutral while the draft is still with the customer, caution
 * while it is out and unanswered, and the provider's own answer once there is one. */
export type NoticeTone = "neutral" | "caution" | "positive" | "critical";

export function noticeTone(notice: Notice): NoticeTone {
  if (notice.status === "provider_responded") {
    if (notice.outcome === "removed") return "positive";
    if (notice.outcome === "refused") return "critical";
    return "caution"; // no_response: still nothing to report
  }
  if (notice.status === "marked_sent_by_customer") return "caution";
  return "neutral"; // draft, customer_approved, withdrawn, corrected
}
