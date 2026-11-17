/**
 * The customer API's response shapes, as types only.
 *
 * Split out of `queries.ts` so the pure logic modules (`caseFile.ts`, `rights.ts`, `invoices.ts`,
 * `submissions.ts`) can name them without reaching through a module that imports TanStack Query
 * and the browser fetch client. The Next.js customer app (ADR 0036) shares exactly these files and
 * typechecks them in a project that has neither dependency.
 *
 * Aliases only — never a request, never a hook. `queries.ts` re-exports them, so existing imports
 * are unchanged.
 */
import type { components } from "../api/schema";

export type Schemas = components["schemas"];

export type CustomerAccount = Schemas["AccountOut"];
export type CustomerSession = Schemas["CustomerSessionOut"];
export type Organisation = Schemas["OrganisationOut"];
export type CustomerOriginal = Schemas["CustomerOriginalOut"];
export type CustomerFinding = Schemas["CustomerCaseFindingOut"];
export type CaseDetail = Schemas["CustomerCaseDetailOut"];
export type CaseStep = Schemas["CaseStepOut"];
export type CaseUnknown = Schemas["CaseUnknownOut"];
export type Removal = Schemas["CustomerRemovalOut"];
// Notice drafts (ADR 0035): what a customer may send, from whom, and what they must confirm.
export type NoticeCandidates = Schemas["NoticeCandidatesOut"];
export type NoticeCandidateFinding = Schemas["NoticeFindingCandidateOut"];
export type NoticeProvider = Schemas["NoticeProviderOut"];
export type NoticeChannel = Schemas["NoticeChannelOut"];
export type Notice = Schemas["NoticeOut"];
export type NoticeFinding = Schemas["NoticeFindingOut"];
export type NoticeConfirmation = Schemas["ConfirmationOut"];
export type RequiredConfirmation = Schemas["RequiredConfirmationOut"];
export type AddOriginalRequest = Schemas["AddOriginalIn"];
export type ClientEventRequest = Schemas["AppClientEventIn"];
