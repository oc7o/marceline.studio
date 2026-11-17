/**
 * The customer case file as pure data: which step is where, who does it, what the evidence says,
 * and which options exist — all as **codes**. The screens turn codes into words in the customer's
 * language; nothing here holds a sentence (ADR 0025: the API ships codes, the UI owns the words).
 *
 * Two rules this module exists to keep in one tested place:
 *
 * - **Nothing is invented.** A date shown is a date the API sent. A step the API reports as
 *   `unavailable` stays unavailable with the API's reason; an option the API has no endpoint for
 *   is shown as not offered, never as a button that pretends to work.
 * - **Nothing claims more than we saw.** "Removed" is when *we observed* the page gone; the words
 *   for who removed it say "the site or its host", because we did not.
 */
import type {
  CaseStep,
  CaseUnknown,
  CustomerAccount,
  CustomerFinding,
  CustomerOriginal,
  Removal,
} from "./apiTypes";

export type StepCode = CaseStep["step"];
export type StepStatus = CaseStep["status"];
export type UnavailableReason = NonNullable<CaseStep["reason"]>;
export type UnknownTopic = CaseUnknown["topic"];
export type OriginalState = CustomerOriginal["state"];
export type EvidenceBasis = CustomerFinding["evidence"];
/**
 * How much of the original a copy carries (issue #62): a fact about the copy, never a confidence
 * and never a reason a copy is or is not shown — excerpts are findings like any other.
 */
export type CopyExtent = CustomerFinding["extent"];

/** Every extent the API can send, spelled out for the words test. */
export const COPY_EXTENTS: readonly CopyExtent[] = ["full_copy", "excerpt", "not_determined"];

/** What shape of work an original is (`matching.Reference.kind`, ADR 0020). */
export type OriginalKind = CustomerOriginal["kind"];

/**
 * The words explaining one copy's extent, for an original of this kind (issue #116, ADR 0053).
 *
 * A photo collection has no timeline and no duration, so `copy_extent` classifies every photo
 * match `unknown` → `not_determined`, whose video sentence ("how much of your video is on this
 * page") says nothing true about a set. That is the only extent a collection ever shows, so it is
 * the only one with a variant — and every word in `core.ts` is paid for by every visitor.
 *
 * An older cached bundle (ADR 0028) carries no `kind` at all; anything that is not `photos` reads
 * as the video wording, which is what every original was before this change.
 */
export function extentExplanationKey(extent: CopyExtent, kind: OriginalKind): string {
  return kind === "photos" && extent === "not_determined"
    ? "finding.extent.not_determined.explanationPhotos"
    : `finding.extent.${extent}.explanation`;
}

/**
 * The customer's steps, in order. Found and verified are one step, `confirmed`, dated to the day:
 * what we are still looking at, and how fast, is internal (operator decision, 2026-09-17).
 */
export const STEP_ORDER: readonly StepCode[] = [
  "confirmed",
  "notice_prepared",
  "notice_sent",
  "notice_acknowledged",
  "removed",
  "rechecked",
];

/**
 * Who a step belongs to. "us" is Marceline, "you" is the customer, "site" is the pirate site or
 * its host. A notice is prepared by us and sent by the customer: sending on their behalf is not
 * offered (#46). Once we send notices ourselves, `notice_sent` becomes "us".
 */
export type Actor = "us" | "you" | "site";

export const STEP_ACTOR: Record<StepCode, Actor> = {
  confirmed: "us",
  notice_prepared: "us",
  notice_sent: "you",
  notice_acknowledged: "site",
  removed: "site",
  rechecked: "us",
};

export interface TimelineEntry {
  step: StepCode;
  status: StepStatus;
  /** A moment (ISO date-time): set when an external step is `done`. */
  at: string | null;
  /** A day (ISO date, no time): set when `confirmed` is `done` — our own step, never finer. */
  on: string | null;
  reason: UnavailableReason | null;
  actor: Actor;
  /** Only on `removed`: the URL came back after this date's removal. */
  reappearedAt: string | null;
}

function done(step: StepCode, at: string | null | undefined): TimelineEntry {
  return {
    step,
    status: at ? "done" : "pending",
    at: at ?? null,
    on: null,
    reason: null,
    actor: STEP_ACTOR[step],
    reappearedAt: null,
  };
}

/**
 * The timeline of **one** copy (one finding) of an original.
 *
 * The API's timeline belongs to the whole case (every URL of an original). For a single copy the
 * steps that concern the URL itself come from that finding's own stamps — `confirmed_on` (a day),
 * `removed_at`, `last_checked` — which the API stores once and never moves. Every finding a
 * customer receives is confirmed, so `confirmed` is always done. The notice steps have no per-URL
 * data at all, so they are taken **verbatim** from the case timeline: today that is `unavailable`
 * with the API's reason, and it must not be rendered as anything else.
 */
export function leakTimeline(finding: CustomerFinding, caseTimeline: readonly CaseStep[]): TimelineEntry[] {
  const fromCase = (step: StepCode): TimelineEntry => {
    const entry = caseTimeline.find((row) => row.step === step);
    if (!entry) {
      // The API promises every step. If one is ever missing, say "not available" rather than
      // "upcoming": nothing we know of will produce it.
      return { step, status: "unavailable", at: null, on: null, reason: null, actor: STEP_ACTOR[step], reappearedAt: null };
    }
    return {
      step,
      status: entry.status,
      at: entry.status === "done" ? (entry.at ?? null) : null,
      on: entry.status === "done" ? (entry.on ?? null) : null,
      reason: entry.status === "unavailable" ? (entry.reason ?? null) : null,
      actor: STEP_ACTOR[step],
      reappearedAt: null,
    };
  };
  return STEP_ORDER.map((step) => {
    switch (step) {
      case "confirmed":
        return { ...done(step, null), status: "done", on: finding.confirmed_on };
      case "removed":
        return { ...done(step, finding.removed_at), reappearedAt: finding.removed_at ? finding.reappeared_at : null };
      case "rechecked":
        return done(step, finding.last_checked);
      default:
        return fromCase(step);
    }
  });
}

/** Online, gone or unknown — the three answers the last availability check can give. */
export type Whereabouts = "online" | "gone" | "unknown";

export function whereabouts(finding: Pick<CustomerFinding, "online">): Whereabouts {
  if (finding.online === true) return "online";
  if (finding.online === false) return "gone";
  return "unknown";
}

/**
 * "Why we think this is yours", as evidence codes in the order a reader needs them: whether the
 * fingerprints match, and the day we confirmed it. The API says no more than that on purpose —
 * no confidence band, no frame count, no percentage: customers get no insight into how sure we
 * are or how we process (operator decision, 2026-09-17).
 */
export type Evidence = { kind: EvidenceBasis } | { kind: "confirmed"; on: string };

/** Every basis the API can send, spelled out for the words test. */
export const EVIDENCE_BASES: readonly EvidenceBasis[] = ["fingerprint_match", "no_fingerprint_match"];

export function evidenceOf(finding: CustomerFinding): Evidence[] {
  return [{ kind: finding.evidence }, { kind: "confirmed", on: finding.confirmed_on }];
}

/**
 * The options of plan §3. None of them has an endpoint today, so none of them can be started; the
 * difference is only *why*, and that is what the customer is told.
 *
 * - `reason` set: the API names this gap in `not_available`, with its reason code.
 * - `reason` null: the API has nothing about it at all — shown as "not offered yet".
 */
export type OptionCode = "prepare_notice" | "search_delisting" | "escalation" | "reupload_watch";

/** Which `not_available` topic, if any, speaks for each option. */
const OPTION_TOPIC: Record<OptionCode, UnknownTopic | null> = {
  prepare_notice: "notice_preparation",
  search_delisting: null,
  escalation: null,
  reupload_watch: "reupload_watch_coverage",
};

export const OPTION_ORDER: readonly OptionCode[] = [
  "prepare_notice",
  "search_delisting",
  "escalation",
  "reupload_watch",
];

export interface CaseOption {
  option: OptionCode;
  reason: UnavailableReason | null;
}

function reasonFor(notAvailable: readonly CaseUnknown[], topic: UnknownTopic | null): UnavailableReason | null {
  if (topic === null) return null;
  return notAvailable.find((row) => row.topic === topic)?.reason ?? null;
}

export function caseOptions(notAvailable: readonly CaseUnknown[]): CaseOption[] {
  return OPTION_ORDER.map((option) => ({ option, reason: reasonFor(notAvailable, OPTION_TOPIC[option]) }));
}

/*
 * Sending a notice for the customer is not offered (in v1 the customer sends the
 * draft from their own account), so the API's `notice_delivery` gap is deliberately not rendered.
 */

/** The escalation ladder of plan §3, bottom rung first. No rung is reached while no notice exists. */
export const ESCALATION_LADDER = ["hoster", "cdn", "registrar", "payment"] as const;
export type EscalationRung = (typeof ESCALATION_LADDER)[number];

/**
 * Hosting (ADR 0027): each finding carries `hosting = {page_host, content_hosts[]}` — who serves
 * the page and its previews, looked up in DNS and RDAP registries. What a customer needs first is
 * the provider, where it sits and the suggested notice channel, the network in front of it, the
 * abuse contact **with its source**, the registrar and how past notices to the provider went; raw
 * addresses and AS numbers are technical details and stay collapsed.
 *
 * Notice history reaches the customer **only as a rounded response history** (operator decision,
 * 2026-09-17): no counts, exact rates or durations. The exact statistics stay internal.
 */
export type Host = CustomerFinding["hosting"]["page_host"];
export type Jurisdiction = Host["jurisdiction"];
export const JURISDICTIONS: readonly Jurisdiction[] = ["eu", "us", "other", "unknown"];
export const LOOKUP_STATES: readonly Host["lookup"][] = ["complete", "partial", "failed", "not_looked_up"];
export const ABUSE_SOURCES: readonly NonNullable<Host["abuse_contact"]>["source"][] = ["ip_network", "registrar"];

/**
 * A provider's response history (ADR 0027 amendment 2026-09-17): what happened to past notices,
 * rounded by the API — the removal share to 5 %, the median time to removal as a floor bucket —
 * or null below the minimum sample ("no response history yet"). Past tense only: never a chance,
 * a band or a forecast, and never a count.
 */
export type ResponseHistory = NonNullable<Host["response_history"]>;
export type RemovalTime = NonNullable<ResponseHistory["usual_removal_time"]>;
/** The API's buckets, fastest first — spelled out for the words test. */
export const REMOVAL_TIMES: readonly RemovalTime[] = [
  "same_day",
  "1_plus_days",
  "3_plus_days",
  "7_plus_days",
  "14_plus_days",
  "30_plus_days",
];

/** The page's own host first, then the preview hosts it does not already name. */
export function hostsOf(finding: Pick<CustomerFinding, "hosting">): { role: "page" | "content"; host: Host }[] {
  const { page_host: pageHost, content_hosts: contentHosts } = finding.hosting;
  const seen = new Set([pageHost.hostname]);
  const rows: { role: "page" | "content"; host: Host }[] = [{ role: "page", host: pageHost }];
  for (const host of contentHosts) {
    if (seen.has(host.hostname)) continue;
    seen.add(host.hostname);
    rows.push({ role: "content", host });
  }
  return rows;
}

/** "DE" → "Germany" / "Deutschland"; the code itself when the browser cannot name it. */
export function countryName(code: string, language: string): string {
  try {
    return new Intl.DisplayNames([language], { type: "region" }).of(code.toUpperCase()) ?? code;
  } catch {
    return code;
  }
}

/** How many sources we watch, as the API's floor bucket (`GET /app/me`). Never a count. */
export type MonitoredSources = CustomerAccount["monitored_sources"];
export const MONITORED_SOURCES: readonly MonitoredSources[] = [
  "fewer_than_10",
  "10_plus",
  "50_plus",
  "100_plus",
  "250_plus",
  "500_plus",
  "1000_plus",
];

/** Overview order: what needs attention first, then what came down, then nothing found. */
const STATE_RANK: Record<OriginalState, number> = { found_online: 0, found_removed: 1, nothing_found: 2 };

export function sortOriginals<T extends Pick<CustomerOriginal, "state">>(originals: readonly T[]): T[] {
  // Stable: within a state the API's order (newest first) stays.
  return [...originals].sort((a, b) => STATE_RANK[a.state] - STATE_RANK[b.state]);
}

export function stateCounts(originals: readonly Pick<CustomerOriginal, "state">[]): Record<OriginalState, number> {
  const counts: Record<OriginalState, number> = { found_online: 0, found_removed: 0, nothing_found: 0 };
  for (const original of originals) counts[original.state] += 1;
  return counts;
}

/**
 * The sites an original's copies sit on, in the API's order and without repeats. Every finding a
 * customer receives is confirmed; unconfirmed matches never reach the browser at all.
 */
export function confirmedSites(findings: readonly CustomerFinding[], originalId: string): string[] {
  const sites: string[] = [];
  for (const finding of findings) {
    if (finding.original_id !== originalId) continue;
    if (!sites.includes(finding.site)) sites.push(finding.site);
  }
  return sites;
}

/** A removals-log entry's standing: back online, removed but the case is open, or handled. */
export type RemovalStanding = "reopened" | "removed_case_open" | "handled";

export function removalStanding(removal: Pick<Removal, "state" | "handled">): RemovalStanding {
  if (removal.state === "reopened") return "reopened";
  return removal.handled ? "handled" : "removed_case_open";
}

/**
 * The kinds of public id a customer URL carries (ADR 0033): `orig_…` names an original, `page_…`
 * the page a copy sits on. Never a database key — an old integer URL is simply not found.
 */
export type PublicIdKind = "orig" | "page";

/**
 * Route params arrive as strings; only a well-formed public id of the expected kind may become an
 * API request (`<kind>_` and 32 lowercase hex digits, the API's own format). Anything else is
 * rendered "not found" without asking — the API would answer the same 404 anyway.
 */
export function parsePublicId(kind: PublicIdKind, value: string | undefined): string | null {
  if (value === undefined) return null;
  return new RegExp(`^${kind}_[0-9a-f]{32}$`).test(value) ? value : null;
}
