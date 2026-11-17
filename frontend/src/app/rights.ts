/**
 * Pure helpers for the rights screen (`/app/rights`, ADR 0034). The API sends codes; the words
 * live in `src/i18n` under `rights.*`, except the declaration texts, which the server owns and the
 * screen shows verbatim — what the customer confirms must be exactly what is stored.
 */
import { ApiError } from "../api/errors";
import type { Schemas } from "./apiTypes";

export type RightsOverview = Schemas["RightsOverviewOut"];
export type RightsWork = Schemas["RightsWorkOut"];
export type RightsPartner = Schemas["LicensedPartnerOut"];
export type RightsProof = Schemas["ProofOut"];
export type ClaimantType = NonNullable<Schemas["RightsClaimantOut"]["claimant_type"]>;
export type RightsStatus = RightsWork["rights_status"];
export type Capacity = Schemas["WorkDeclarationIn"]["capacity"];
export type AssertedRight = Schemas["WorkDeclarationIn"]["asserted_right"];
export type PartnerKind = Schemas["PartnerIn"]["kind"];
export type Blocker = RightsWork["notice_blockers"][number];
export type IdentityStatus = Schemas["RightsClaimantOut"]["identity_status"];
export type IdentityMethod = NonNullable<Schemas["RightsClaimantOut"]["identity_method"]>;
export type ProofKind = RightsProof["kind"];
export type Language = Schemas["WorkDeclarationIn"]["language"];
export type WithdrawalReason = Schemas["WithdrawalIn"]["reason"];

export const CLAIMANT_TYPES: ClaimantType[] = [
  "studio_producer",
  "self_producing_creator",
  "agency",
  "studio_performer",
  "collaboration",
];
export const CAPACITIES: Capacity[] = ["owner", "authorised_agent"];
export const ASSERTED_RIGHTS: AssertedRight[] = [
  "copyright",
  "film_producer_right",
  "exclusive_licence",
  "performer_right",
  "joint_right",
  "other",
];
export const WITHDRAWAL_REASONS: WithdrawalReason[] = [
  "rights_transferred",
  "authority_ended",
  "declared_in_error",
  "other",
];
export const PARTNER_KINDS: PartnerKind[] = ["domain", "platform", "distributor", "affiliate"];
export const PROOF_KINDS: ProofKind[] = [
  "contract_excerpt",
  "licence",
  "assignment",
  "agency_mandate",
  "catalogue_schedule",
  "company_register",
  "publication_record",
  "other",
];
export const IDENTITY_STATUSES: IdentityStatus[] = ["unverified", "verified", "revoked"];
export const IDENTITY_METHODS: IdentityMethod[] = [
  "commercial_register",
  "company_email",
  "account_control_token",
  "other",
];
export const BLOCKERS: Blocker[] = [
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
];

export const RIGHTS_STATUS_TONES: Record<RightsStatus, string> = {
  unverified: "bg-slate-100 text-slate-700",
  self_declared: "bg-sky-100 text-sky-900",
  documents_received: "bg-sky-100 text-sky-900",
  documents_complete: "bg-sky-100 text-sky-900",
  authority_declared: "bg-emerald-100 text-emerald-900",
  disputed: "bg-amber-100 text-amber-900",
  revoked: "bg-rose-100 text-rose-900",
  expired: "bg-rose-100 text-rose-900",
  superseded: "bg-slate-100 text-slate-700",
};
export const RIGHTS_STATUSES = Object.keys(RIGHTS_STATUS_TONES) as RightsStatus[];

/** Refusal codes the rights endpoints answer with (409, 413, 415, 503), each with words. */
export const ERROR_CODES = [
  "claimant_type_locked",
  "declaration_not_confirmed",
  "declaration_version_outdated",
  "declaration_not_open",
  "declaration_not_withdrawable",
  "authority_not_declarable",
  "authority_not_withdrawable",
  "underlying_owner_required",
  "asserted_right_detail_required",
  "invalid_domain",
  "partner_domain_required",
  "partner_name_required",
  "partners_exist",
  "too_large",
  "unsupported_type",
  "empty",
  "unavailable",
] as const;
export type ErrorCode = (typeof ERROR_CODES)[number];

/** A write answered 202 (production, ADR 0028 write path): accepted into the outbox, not yet
 * applied. Development applies it and the overview picks it up on the next fetch. */
export function isPendingWrite(result: unknown): result is Schemas["RightsWriteAcceptedOut"] {
  return !!result && typeof result === "object" && "pending" in result;
}

/** The i18n key for a failed write: the refusal's own words when it has any, else a generic line. */
export function errorKey(error: unknown): string {
  if (error instanceof ApiError && error.status !== 404) {
    const code = error.message;
    if ((ERROR_CODES as readonly string[]).includes(code)) return `rights.errors.codes.${code}`;
  }
  return "rights.errors.generic";
}

/** The declaration text in the UI language; German for "de", English otherwise. */
export function declarationLanguage(language: string): Language {
  return language.startsWith("de") ? "de" : "en";
}

/** "10 MB" / "512 KB": the upload limit as a person reads it. */
export function formatBytes(bytes: number, language: string): string {
  const megabytes = bytes / (1024 * 1024);
  if (megabytes >= 1) {
    return `${new Intl.NumberFormat(language, { maximumFractionDigits: 1 }).format(megabytes)} MB`;
  }
  return `${new Intl.NumberFormat(language, { maximumFractionDigits: 0 }).format(bytes / 1024)} KB`;
}

/** A work with no declaration yet may be declared; a self-declared one may be updated. */
export function canDeclare(work: RightsWork): boolean {
  return work.rights_status === "unverified" || work.rights_status === "self_declared";
}

/** Only a self-declared work can be withdrawn by the customer; after documents, only staff. */
export function canWithdraw(work: RightsWork): boolean {
  return work.rights_status === "self_declared";
}

/**
 * Once our staff have recorded the documents as formally complete, the customer — and only the
 * customer — states that those documents give them the right or authority they declared (ADR 0034
 * amendment 2026-09-18). There is no staff route to it anywhere in the product.
 */
export function canDeclareAuthority(work: RightsWork): boolean {
  return work.rights_status === "documents_complete";
}

/** And they can take that statement back; the documents stay on file. */
export function canWithdrawAuthority(work: RightsWork): boolean {
  return work.rights_status === "authority_declared";
}

/** The multipart body of a proof upload; the file itself never leaves this form's request. */
export function proofForm(file: File, kind: ProofKind, originalId: string | null, note: string): FormData {
  const form = new FormData();
  form.append("file", file);
  form.append("kind", kind);
  if (originalId) form.append("original_id", originalId);
  if (note.trim()) form.append("note", note.trim());
  return form;
}
