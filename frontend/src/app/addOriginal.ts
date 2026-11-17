/**
 * The "Add original" page's logic, pure and tested: the cast a customer typed, what is missing
 * from the form, and which words a failure gets.
 *
 * The server sends codes (`detail`), the UI owns the words (ADR 0026). Nothing here imports the
 * fingerprinting code: failures from it are recognised by their `name` and `reason`, so this
 * module and the form stay in the page's small chunk and the Worker loads only on "start".
 */
import { ApiError } from "../api/errors";
import { MAX_UPLOAD_IMAGES } from "../lib/photoFingerprintFormat";
import { MAX_CREATORS, MAX_TITLE_LENGTH } from "../lib/videoFingerprintFormat";

/**
 * The version of the rights declaration this UI shows. The words are `addOriginal.rights.statement`
 * in both languages; `addOriginal.test.ts` checks them against the server's
 * `backend/marceline/app/rights_confirmation.json`, whose `current` must be this version.
 *
 * `2026-09-18` says *original* where `2026-09-17` said *video*: an original is a video **or** a
 * photo collection since issue #116, and a declaration has to be true for the thing it stands next
 * to. A new wording is a new version on both sides, never an edit (ADR 0053 §4).
 */
export const RIGHTS_TEXT_VERSION = "2026-09-18";

/**
 * The version of the **upload** declaration (cloud fingerprinting, ADR 0044 amendment 2026-09-19).
 * Shown only when the customer chooses to send the file: they declare they are the only person in
 * it and consent explicitly to the processing of it (Art. 9(2)(a) GDPR). The words are
 * `addOriginal.upload.declaration.solePerformer` / `.article9Consent` in both languages;
 * `addOriginal.test.ts` checks them against the server's
 * `backend/marceline/app/upload_declaration.json`, whose `current` must be this version.
 *
 * **Draft wording** — like everything about this feature, it is behind
 * `CLOUD_FINGERPRINTING_ENABLED`, which is off.
 */
export const UPLOAD_DECLARATION_VERSION = "upload-2026-09-19";

/** What the customer is adding. `matching.Reference.kind`, and the one thing the form branches on. */
export type OriginalKind = "video" | "photos";

/** One name as the server stores it: whitespace collapsed. */
export function normaliseName(name: string): string {
  return name.split(/\s+/).filter(Boolean).join(" ");
}

/**
 * Rights holder first, then every other non-empty line, each name once (ignoring case, like the
 * server). An empty rights holder gives an empty cast: nobody is promoted in their place.
 */
export function castFrom(rightsHolder: string, others: string): string[] {
  const primary = normaliseName(rightsHolder);
  if (!primary) return [];
  const seen = new Set([primary.toLocaleLowerCase()]);
  const cast = [primary];
  for (const line of others.split(/\r?\n/)) {
    const name = normaliseName(line);
    const key = name.toLocaleLowerCase();
    if (name && !seen.has(key)) {
      seen.add(key);
      cast.push(name);
    }
  }
  return cast;
}

export interface FormState {
  /** The one video, when a video is being added. */
  file: File | null;
  /** The photographs, when a set is being added (ADR 0053); absent for a video. */
  files?: readonly File[];
  title: string;
  rightsHolder: string;
  others: string;
  confirmed: boolean;
  /**
   * The upload declaration's two ticks, and whether it is being asked for at all (ADR 0044
   * amendment 2026-09-19). Absent — a local fingerprint — means it is not asked for and not
   * required: nothing about anybody is processed by us when the file never leaves the machine.
   */
  sending?: boolean;
  solePerformer?: boolean;
  article9Consent?: boolean;
}

/** Translation keys under `addOriginal.problems`, in the order the form shows its fields. */
export type FormProblem =
  | "missingFile"
  | "tooManyImages"
  | "missingTitle"
  | "titleTooLong"
  | "missingRightsHolder"
  | "tooManyNames"
  | "nameTooLong"
  | "notConfirmed"
  | "notDeclared";

export function formProblems(form: FormState): FormProblem[] {
  const problems: FormProblem[] = [];
  const cast = castFrom(form.rightsHolder, form.others);
  const title = form.title.trim();
  const chosen = form.files?.length ?? 0;
  if (form.file === null && chosen === 0) problems.push("missingFile");
  if (chosen > MAX_UPLOAD_IMAGES) problems.push("tooManyImages");
  if (!title) problems.push("missingTitle");
  if (title.length > MAX_TITLE_LENGTH) problems.push("titleTooLong");
  if (cast.length === 0) problems.push("missingRightsHolder");
  if (cast.length > MAX_CREATORS) problems.push("tooManyNames");
  if (cast.some((name) => name.length > MAX_TITLE_LENGTH)) problems.push("nameTooLong");
  if (!form.confirmed) problems.push("notConfirmed");
  // Both ticks, or the file is not sent. One message for the pair: the form asks for two
  // statements because they are two different things, and the customer needs one instruction.
  if (form.sending && !(form.solePerformer && form.article9Consent)) problems.push("notDeclared");
  return problems;
}

/** A failure as the page words it: a key under `addOriginal.errors` and what it interpolates. */
export interface FailureMessage {
  key: string;
  count?: number;
}

/** Local reasons with words of their own; every other local failure is one generic sentence. */
const LOCAL_REASONS = new Set([
  "cancelled",
  "too_long",
  "no_duration",
  "no_usable_frames",
  "too_many_images",
]);
/**
 * The two local reasons whose sentence depends on what was being added: "the video has too few
 * distinct images" is nonsense about a photo set, and so is the advice about MP4 and H.264. Every
 * other reason is either kind-neutral already or can only happen to one kind.
 */
const PHOTO_KEYS = {
  no_usable_frames: "addOriginal.photos.errors.noUsableImages",
  codec: "addOriginal.photos.errors.codecUnsupported",
} as const;

/** A failure while reading or hashing the customer's files on this computer. */
export function localFailure(error: unknown, kind: OriginalKind = "video"): FailureMessage {
  const photos = kind === "photos";
  if (error instanceof Error && error.name === "CodecUnsupportedError") {
    return { key: photos ? PHOTO_KEYS.codec : "addOriginal.errors.codecUnsupported" };
  }
  const reason = error instanceof Error && "reason" in error ? String(error.reason) : "";
  if (photos && reason === "no_usable_frames") return { key: PHOTO_KEYS.no_usable_frames };
  if (LOCAL_REASONS.has(reason)) return { key: `addOriginal.errors.local.${reason}` };
  return { key: "addOriginal.errors.local.other" };
}

/** The words for one form problem, which only "choose a file" words differently per kind. */
export function problemKey(problem: FormProblem, kind: OriginalKind): string {
  if (kind === "photos" && problem === "missingFile") {
    return "addOriginal.photos.problems.missingFiles";
  }
  return `addOriginal.problems.${problem}`;
}

/** The server's refusal codes that have words of their own (`AddOriginalErrorOut.detail`). */
const SERVER_CODES = new Set([
  "claim_needs_review",
  "rights_confirmation_required",
  "too_long",
  // A photo set past `MAX_IMAGES` (ADR 0053): its own code, because the advice is not a video's.
  "too_many_images",
  "adding_originals_unavailable",
  "processing_paused",
  "no_organisation",
  "body_too_large",
  // Cloud fingerprinting (ADR 0044): the two refusals of `POST /app/uploads…` whose advice
  // differs from "try again" — wait a day, or wait a while. Its other codes (a mismatched digest,
  // a file that is too large, uploads switched off here) fall to the generic sentences below,
  // which already say the right thing, and every string the customer UI carries is paid for in
  // the SPA's first-load budget by every visitor (`frontend/scripts/check-bundle-budget.mjs`).
  "quota_exceeded",
  "spool_full",
]);

/** A failure while sending the fingerprints, from the status and the code the API answered. */
export function uploadFailure(error: unknown): FailureMessage {
  if (!(error instanceof ApiError)) return { key: "addOriginal.errors.server.unreachable" };
  if (error.status === 429) {
    const seconds = error.retryAfterSeconds;
    return seconds === null
      ? { key: "addOriginal.errors.server.too_many_requests" }
      : { key: "addOriginal.errors.server.too_many_requests_minutes", count: Math.max(1, Math.ceil(seconds / 60)) };
  }
  if (error.status === 401) return { key: "addOriginal.errors.server.signed_out" };
  const body = error.body;
  const code = body && typeof body === "object" && "detail" in body ? String(body.detail) : "";
  if (SERVER_CODES.has(code)) return { key: `addOriginal.errors.server.${code}` };
  if (error.status === 400 || error.status === 422) return { key: "addOriginal.errors.server.invalid" };
  return { key: "addOriginal.errors.server.unexpected" };
}

/** Every failure key this module can produce, for the test that requires words in both languages. */
export const FAILURE_KEYS: readonly string[] = [
  "addOriginal.errors.codecUnsupported",
  ...Object.values(PHOTO_KEYS),
  ...[...LOCAL_REASONS, "other"].map((reason) => `addOriginal.errors.local.${reason}`),
  ...[
    ...SERVER_CODES,
    "unreachable",
    "too_many_requests",
    "too_many_requests_minutes",
    "signed_out",
    "invalid",
    "unexpected",
  ].map((code) => `addOriginal.errors.server.${code}`),
];

/** "1.4 GB" / "1,4 GB": a file size for a person, in the page's language. */
export function formatFileSize(bytes: number, language: string): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  const digits = unit === 0 || value >= 100 ? 0 : 1;
  return `${new Intl.NumberFormat(language, { maximumFractionDigits: digits }).format(value)} ${units[unit]}`;
}
