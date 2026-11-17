/**
 * The status-badge tones of this app, on the design system's tokens (`app/globals.css`).
 *
 * They live here rather than in `@shared/app/*` on purpose: the shared modules are read by the Vite
 * SPA too, which has no token layer, so a badge there still wants Tailwind's own palette. The words,
 * the codes and every piece of logic stay shared — only the class strings are ours, and the maps are
 * keyed by the shared types, so a new status code fails `tsc` here as well.
 *
 * Three rules of the system are applied, and they are the reason these are not one grey:
 *
 * - **Colour never works alone.** Every badge that uses one of these also renders its status as a
 *   word; the ground only repeats it.
 * - **Automation is violet.** `accent` marks what the machine did — an upload still being
 *   fingerprinted, a notice the server drafted — and nothing a person decided.
 * - **Spend the red.** `critical` is for something that needs the customer: a copy back online, an
 *   overdue invoice's rights record revoked. Ordinary states are neutral.
 */
import type { RemovalStanding } from "@shared/app/caseFile";
import type { InvoiceStatus } from "@shared/app/invoices";
import type { NoticeStatus, NoticeSubmission } from "@shared/app/notices";
import type { RightsStatus } from "@shared/app/rights";
import type { Submission } from "@shared/app/submissions";

/** Removals log: gone for good, gone but the record is open, or back online. */
export const STANDING_TONES: Record<RemovalStanding, string> = {
  handled: "bg-positive-quiet text-positive-ink",
  removed_case_open: "bg-surface-300 text-ink",
  reopened: "bg-critical-quiet text-critical-ink",
};

/** The customer's own uploads. `processing` is the machine working, so it is the violet one. */
export const SUBMISSION_TONES: Record<Submission["status"], string> = {
  processing: "bg-accent-quiet text-accent",
  done: "bg-positive-quiet text-positive-ink",
  refused: "bg-surface-300 text-ink",
  failed: "bg-caution-quiet text-caution-ink",
};

/** Notice writes still pending, or refused after acceptance (ADR 0046 §7, issue #86). `done` is
 * never shown (the block filters it out before rendering) but is typed like the upload one above,
 * for the same reason: the status is the API's own type, not a narrowed one. */
export const NOTICE_SUBMISSION_TONES: Record<NoticeSubmission["status"], string> = {
  processing: "bg-accent-quiet text-accent",
  done: "bg-positive-quiet text-positive-ink",
  refused: "bg-surface-300 text-ink",
  failed: "bg-caution-quiet text-caution-ink",
};

/** Invoices (ADR 0032). Overdue is caution, not red: red stays with the matching copies. */
export const INVOICE_TONES: Record<InvoiceStatus, string> = {
  issued: "bg-surface-200 text-ink-muted",
  open: "bg-surface-300 text-ink",
  overdue: "bg-caution-quiet text-caution-ink",
  paid: "bg-positive-quiet text-positive-ink",
  cancelled: "bg-surface-200 text-ink-subtle",
};

/** Rights records (ADR 0034). What a person decided is neutral; what ended is red. */
export const RIGHTS_TONES: Record<RightsStatus, string> = {
  unverified: "bg-surface-200 text-ink-muted",
  self_declared: "bg-surface-300 text-ink",
  documents_received: "bg-surface-300 text-ink",
  documents_complete: "bg-surface-300 text-ink",
  authority_declared: "bg-positive-quiet text-positive-ink",
  disputed: "bg-caution-quiet text-caution-ink",
  revoked: "bg-critical-quiet text-critical-ink",
  expired: "bg-critical-quiet text-critical-ink",
  superseded: "bg-surface-200 text-ink-subtle",
};

/**
 * Notice drafts (ADR 0035). `draft` is violet because the server composed that text from stored
 * facts; everything after it is the customer's own act and reads neutral.
 */
export const NOTICE_TONES: Record<NoticeStatus, string> = {
  draft: "bg-accent-quiet text-accent",
  customer_approved: "bg-surface-300 text-ink",
  marked_sent_by_customer: "bg-surface-300 text-ink",
  provider_responded: "bg-positive-quiet text-positive-ink",
  withdrawn: "bg-surface-200 text-ink-subtle",
  corrected: "bg-surface-200 text-ink-subtle",
};
