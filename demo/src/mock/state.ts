/**
 * The demo's whole "database": invented sample data for one organisation, held in memory.
 *
 * Everything here is made up. Sites are on `.example`, addresses come from the documentation
 * ranges (192.0.2.0/24, 2001:db8::/32, AS64496–64511) and no person, company or site is real.
 * A reload starts again from this file.
 */
import type { components } from "../../../frontend/src/api/schema";

export type Schemas = components["schemas"];
export type Host = Schemas["CustomerHostOut"];
export type Notice = Schemas["NoticeOut"];
export type Finding = Schemas["CustomerCaseFindingOut"];
export type Original = Schemas["CustomerOriginalOut"];
export type RightsWork = Schemas["RightsWorkOut"];
export type ChannelKind = Schemas["NoticeChannelOut"]["kind"];

/** A public id as the API sends it (ADR 0033): a prefix and 32 hex digits, stable per seed. */
export function publicId(prefix: string, seed: number): string {
  let state = (seed * 2654435761 + prefix.length * 40503) >>> 0;
  let hex = "";
  while (hex.length < 32) {
    state = (Math.imul(state ^ (state >>> 15), 2246822507) + 0x9e3779b9) >>> 0;
    hex += state.toString(16).padStart(8, "0");
  }
  return `${prefix}_${hex}`;
}

let serial = 1000;
/** Ids that outlived a reload (see `rememberAdded`): the counter starts over, they must not be issued twice. */
const taken = new Set<string>();
export function nextId(prefix: string): string {
  let id: string;
  do {
    serial += 1;
    id = publicId(prefix, serial);
  } while (taken.has(id));
  return id;
}

export function nowIso(): string {
  return new Date().toISOString();
}

export const ORGANISATION: Schemas["OrganisationOut"] = {
  id: publicId("org", 1),
  name: "Novak Studio",
  segment: "professional",
  role: "owner",
  created_at: "2026-07-28T09:30:00Z",
};

export const USERNAME = "demo@novak-studio.example";

// ---------------------------------------------------------------------------
// Hosts
// ---------------------------------------------------------------------------
const RDAP = "https://rdap.registry.example";

const HOSTS = {
  clipvault: {
    hostname: "clipvault.example",
    lookup: "complete",
    looked_up_at: "2026-10-26T05:10:00Z",
    stale: false,
    addresses: ["192.0.2.14", "2001:db8:14::1"],
    asn: 64500,
    as_name: "EXAMPLE-HOSTING-AS",
    provider: "Example Hosting GmbH",
    country: "DE",
    jurisdiction: "eu",
    cdn: null,
    abuse_contact: {
      email: "abuse@example-hosting.example",
      source: "ip_network",
      source_url: `${RDAP}/ip/192.0.2.0/26`,
    },
    registrar: {
      name: "Sample Registrar Ltd",
      abuse_email: "abuse@sample-registrar.example",
      source_url: `${RDAP}/domain/clipvault.example`,
    },
    response_history: { removed_percent: 80, usual_removal_time: "3_plus_days" },
  },
  streamdepot: {
    hostname: "streamdepot.example",
    lookup: "complete",
    looked_up_at: "2026-10-25T04:40:00Z",
    stale: false,
    addresses: ["192.0.2.77", "2001:db8:77::20"],
    asn: 64501,
    as_name: "SAMPLE-CLOUD",
    provider: "Sample Cloud Inc.",
    country: "US",
    jurisdiction: "us",
    cdn: "Placeholder CDN",
    abuse_contact: {
      email: "copyright@sample-cloud.example",
      source: "ip_network",
      source_url: `${RDAP}/ip/192.0.2.64/26`,
    },
    registrar: {
      name: "Sample Registrar Ltd",
      abuse_email: "abuse@sample-registrar.example",
      source_url: `${RDAP}/domain/streamdepot.example`,
    },
    response_history: { removed_percent: 60, usual_removal_time: "7_plus_days" },
  },
  streamdepotCdn: {
    hostname: "media.streamdepot.example",
    lookup: "complete",
    looked_up_at: "2026-10-25T04:41:00Z",
    stale: false,
    addresses: ["192.0.2.90"],
    asn: 64503,
    as_name: "PLACEHOLDER-CDN",
    provider: "Placeholder CDN",
    country: "US",
    jurisdiction: "us",
    cdn: "Placeholder CDN",
    abuse_contact: {
      email: "abuse@placeholder-cdn.example",
      source: "ip_network",
      source_url: `${RDAP}/ip/192.0.2.88/29`,
    },
    registrar: null,
    response_history: null,
  },
  reuploads: {
    hostname: "reuploads.example",
    lookup: "partial",
    looked_up_at: "2026-09-18T06:00:00Z",
    stale: true,
    addresses: ["192.0.2.131"],
    asn: 64502,
    as_name: "DOC-NET-64502",
    provider: null,
    country: "NL",
    jurisdiction: "eu",
    cdn: null,
    abuse_contact: null,
    registrar: {
      name: "Demo Domains B.V.",
      abuse_email: "abuse@demo-domains.example",
      source_url: `${RDAP}/domain/reuploads.example`,
    },
    response_history: { removed_percent: 40, usual_removal_time: "14_plus_days" },
  },
  videodump: {
    hostname: "videodump.example",
    lookup: "failed",
    looked_up_at: "2026-10-20T03:15:00Z",
    stale: false,
    addresses: [],
    asn: null,
    as_name: null,
    provider: null,
    country: null,
    jurisdiction: "unknown",
    cdn: null,
    abuse_contact: null,
    registrar: null,
    response_history: null,
  },
  picboard: {
    hostname: "picboard.example",
    lookup: "not_looked_up",
    looked_up_at: null,
    stale: false,
    addresses: [],
    asn: null,
    as_name: null,
    provider: null,
    country: null,
    jurisdiction: "unknown",
    cdn: null,
    abuse_contact: null,
    registrar: null,
    response_history: null,
  },
} satisfies Record<string, Host>;

type HostKey = keyof typeof HOSTS;

// ---------------------------------------------------------------------------
// Originals and the pages they were found on
// ---------------------------------------------------------------------------
export interface OriginalRow {
  id: string;
  title: string;
  kind: Original["kind"];
  duration_s: number | null;
  created_at: string;
}

const ORIG = {
  lisbon: publicId("orig", 1),
  ramen: publicId("orig", 2),
  acoustic: publicId("orig", 3),
  mobility: publicId("orig", 4),
  grading: publicId("orig", 5),
  dolomites: publicId("orig", 6),
  sourdough: publicId("orig", 7),
};

export const originals: OriginalRow[] = [
  { id: ORIG.sourdough, title: "Sourdough starter, day by day", kind: "video", duration_s: 640, created_at: "2026-10-06T08:20:00Z" },
  { id: ORIG.dolomites, title: "Dolomites hut-to-hut — photo set", kind: "photos", duration_s: null, created_at: "2026-09-21T17:05:00Z" },
  { id: ORIG.grading, title: "Colour grading basics — a beginner tutorial", kind: "video", duration_s: 1975, created_at: "2026-09-09T10:45:00Z" },
  { id: ORIG.mobility, title: "20-minute morning mobility routine", kind: "video", duration_s: 1218, created_at: "2026-09-02T06:30:00Z" },
  { id: ORIG.acoustic, title: "Acoustic session — three songs live", kind: "video", duration_s: 1045, created_at: "2026-08-19T19:10:00Z" },
  { id: ORIG.ramen, title: "Weeknight ramen from scratch", kind: "video", duration_s: 1260, created_at: "2026-08-11T12:00:00Z" },
  { id: ORIG.lisbon, title: "Lisbon in 48 hours — travel vlog", kind: "video", duration_s: 872, created_at: "2026-08-04T15:40:00Z" },
];

/** One reviewed match: everything a finding row needs except what is derived from its siblings. */
export interface FindingRow extends Omit<Finding, "case_state"> {
  host: HostKey;
  requires_exception_review: boolean;
}

function found(
  seed: number,
  originalId: string,
  host: HostKey,
  path: string,
  confirmedOn: string,
  rest: Partial<FindingRow> = {},
): FindingRow {
  const page = HOSTS[host];
  return {
    original_id: originalId,
    page_id: publicId("page", seed),
    site: page.hostname,
    url: `https://${page.hostname}${path}`,
    availability: "playable",
    online: true,
    last_checked: "2026-10-27T06:12:00Z",
    confirmed_on: confirmedOn,
    removed_at: null,
    reappeared_at: null,
    evidence: "fingerprint_match",
    extent: "full_copy",
    hosting: { page_host: page, content_hosts: [] },
    host,
    requires_exception_review: false,
    ...rest,
  };
}

const PAGE = { lisbonClipvault: 101, lisbonStreamdepot: 102, lisbonReuploads: 103, ramenClipvault: 201, ramenVideodump: 202, acousticStreamdepot: 301, acousticClipvault: 302, gradingReuploads: 501, dolomitesPicboard: 601 };

export const findings: FindingRow[] = [
  found(PAGE.lisbonClipvault, ORIG.lisbon, "clipvault", "/watch/lisbon-in-48-hours-full", "2026-09-03"),
  found(PAGE.lisbonStreamdepot, ORIG.lisbon, "streamdepot", "/v/83121", "2026-09-03", {
    extent: "excerpt",
    requires_exception_review: true,
    last_checked: "2026-10-26T18:30:00Z",
    hosting: { page_host: HOSTS.streamdepot, content_hosts: [HOSTS.streamdepotCdn] },
  }),
  found(PAGE.lisbonReuploads, ORIG.lisbon, "reuploads", "/clip/a7f3e", "2026-09-08", {
    availability: "removed",
    online: false,
    removed_at: "2026-09-19T07:40:00Z",
    last_checked: "2026-10-24T07:40:00Z",
  }),
  found(PAGE.ramenClipvault, ORIG.ramen, "clipvault", "/watch/ramen-from-scratch", "2026-09-15"),
  found(PAGE.ramenVideodump, ORIG.ramen, "videodump", "/post/55102", "2026-09-22", {
    evidence: "no_fingerprint_match",
    extent: "not_determined",
    last_checked: "2026-10-25T11:05:00Z",
  }),
  found(PAGE.acousticStreamdepot, ORIG.acoustic, "streamdepot", "/v/79004", "2026-08-28", {
    availability: "removed",
    online: false,
    removed_at: "2026-09-16T09:15:00Z",
    last_checked: "2026-10-26T09:15:00Z",
    hosting: { page_host: HOSTS.streamdepot, content_hosts: [HOSTS.streamdepotCdn] },
  }),
  found(PAGE.acousticClipvault, ORIG.acoustic, "clipvault", "/watch/acoustic-session-live", "2026-08-28", {
    availability: "removed",
    online: false,
    removed_at: "2026-09-06T14:20:00Z",
    last_checked: "2026-10-26T09:16:00Z",
  }),
  found(PAGE.gradingReuploads, ORIG.grading, "reuploads", "/clip/c19d2", "2026-09-25", {
    extent: "excerpt",
    removed_at: "2026-10-02T08:00:00Z",
    reappeared_at: "2026-10-14T16:45:00Z",
    last_checked: "2026-10-27T05:50:00Z",
  }),
  found(PAGE.dolomitesPicboard, ORIG.dolomites, "picboard", "/album/dolomites-hut-to-hut", "2026-10-09", {
    extent: "not_determined",
    last_checked: "2026-10-26T20:00:00Z",
  }),
];

// ---------------------------------------------------------------------------
// Rights
// ---------------------------------------------------------------------------
export const DECLARATIONS = {
  work: {
    version: "work-2026-09-17",
    en: "I declare that the organisation I act for holds the right stated above in this work, or is currently authorised by the rights holder named above to act in relation to that right. To my knowledge, this right or authority has not been revoked or transferred. I will update this record promptly if that changes. I understand that this declaration is recorded as a self-declaration: it is not proof of the rights and does not by itself make notice drafts available.",
    de: "Ich erkläre, dass die Organisation, für die ich handle, das oben angegebene Recht an diesem Werk innehat oder derzeit von der oben genannten Rechteinhaberin bzw. dem oben genannten Rechteinhaber ermächtigt ist, in Bezug auf dieses Recht zu handeln. Nach meiner Kenntnis wurde dieses Recht bzw. diese Ermächtigung weder widerrufen noch übertragen. Ändert sich das, aktualisiere ich diesen Eintrag unverzüglich. Mir ist bewusst, dass diese Erklärung als Selbstauskunft gespeichert wird: Sie ist kein Nachweis der Rechte und ermöglicht für sich allein keine Meldungsentwürfe.",
  },
  partners: {
    version: "partners-none-2026-09-17",
    en: "I have reviewed the licences and distribution agreements of the organisation I act for. It has no licensed partners, authorised distributors, affiliates or platforms that are permitted to publish its works. I will add any such partner here as soon as one exists.",
    de: "Ich habe die Lizenz- und Vertriebsvereinbarungen der Organisation, für die ich handle, geprüft. Sie hat keine Lizenzpartner, autorisierten Vertriebspartner, Affiliates oder Plattformen, die ihre Werke veröffentlichen dürfen. Sobald es einen solchen Partner gibt, trage ich ihn hier ein.",
  },
  withdrawal: {
    version: "withdraw-2026-09-17",
    en: "I withdraw the self-declaration for this work. From now on the organisation I act for makes no statement about its rights to this work in Marceline, until it declares them again.",
    de: "Ich ziehe die Selbstauskunft zu diesem Werk zurück. Die Organisation, für die ich handle, macht in Marceline ab sofort keine Angabe mehr zu ihren Rechten an diesem Werk, bis sie diese erneut erklärt.",
  },
  authority: {
    version: "authority-2026-09-18",
    en: "I have read the documents the organisation I act for has given Marceline for this work, and I state that they give it the right or the authority I declared here. Where I act as an authorised agent, the mandate I rely on covers preparing and submitting notices about this work. Nobody at Marceline has assessed for me what these documents mean in law; where I needed that, I took my own advice. I will withdraw this declaration promptly if it stops being true.",
    de: "Ich habe die Unterlagen gelesen, die die Organisation, für die ich handle, Marceline zu diesem Werk übergeben hat, und erkläre, dass sie ihr das Recht bzw. die Befugnis geben, die ich hier angegeben habe. Soweit ich als Bevollmächtigte handle, umfasst die Vollmacht, auf die ich mich stütze, das Vorbereiten und Einreichen von Meldungen zu diesem Werk. Niemand bei Marceline hat für mich geprüft, was diese Unterlagen rechtlich bedeuten; wo ich das brauchte, habe ich mir selbst Rat eingeholt. Ich ziehe diese Erklärung unverzüglich zurück, wenn sie nicht mehr zutrifft.",
  },
  authorityWithdrawal: {
    version: "authority-withdraw-2026-09-18",
    en: "I withdraw my declaration about the right or authority our documents give for this work. Until the organisation I act for declares it again, it makes no such statement in Marceline. The documents themselves stay on file.",
    de: "Ich ziehe meine Erklärung dazu zurück, welches Recht bzw. welche Befugnis unsere Unterlagen für dieses Werk geben. Bis die Organisation, für die ich handle, sie erneut erklärt, macht sie dazu in Marceline keine Angabe. Die Unterlagen selbst bleiben gespeichert.",
  },
} satisfies Record<string, Schemas["DeclarationTextOut"]>;

function work(originalId: string, overrides: Partial<RightsWork>): RightsWork {
  return {
    original_id: originalId,
    title: originals.find((row) => row.id === originalId)?.title ?? "",
    rights_status: "authority_declared",
    capacity: "owner",
    underlying_owner_name: null,
    asserted_right: "copyright",
    asserted_right_detail: "",
    declared_at: "2026-09-18T10:00:00Z",
    declaration_version: DECLARATIONS.work.version,
    authority_declared_at: "2026-09-24T09:00:00Z",
    authority_declaration_version: DECLARATIONS.authority.version,
    notice_blockers: [],
    ...overrides,
  };
}

const UNDECLARED: Partial<RightsWork> = {
  rights_status: "unverified",
  capacity: null,
  asserted_right: null,
  declared_at: null,
  declaration_version: null,
  authority_declared_at: null,
  authority_declaration_version: null,
  notice_blockers: ["rights_record_missing"],
};

export function undeclaredWork(originalId: string, title: string): RightsWork {
  return { ...work(originalId, UNDECLARED), title };
}

export function addOriginal(row: OriginalRow): void {
  originals.unshift(row);
  rights.works.push(undeclaredWork(row.id, row.title));
}

/*
  An original added in the tab has an address of its own (`/app/originals/{id}`), and the screen
  that follows "Add an original" links straight to it. Everything else a reload resets is still
  on a page the visitor can reach; this one would be "not found". So the row — a title, a kind and
  a length, never the file — stays in the tab's session storage until the tab closes.
*/
const ADDED_KEY = "marceline.demo.added";

function addedRows(): OriginalRow[] {
  try {
    const stored: unknown = JSON.parse(globalThis.sessionStorage?.getItem(ADDED_KEY) ?? "[]");
    if (!Array.isArray(stored)) return [];
    return stored.filter(
      (row): row is OriginalRow =>
        typeof row === "object" &&
        row !== null &&
        /^orig_[0-9a-f]{32}$/.test(String((row as OriginalRow).id)) &&
        typeof (row as OriginalRow).title === "string" &&
        typeof (row as OriginalRow).created_at === "string" &&
        ((row as OriginalRow).kind === "video" || (row as OriginalRow).kind === "photos") &&
        ((row as OriginalRow).duration_s === null || typeof (row as OriginalRow).duration_s === "number"),
    );
  } catch {
    return [];
  }
}

export function rememberAdded(row: OriginalRow): void {
  try {
    globalThis.sessionStorage?.setItem(ADDED_KEY, JSON.stringify([...addedRows(), row]));
  } catch {
    // Storage is blocked or full: the original still exists until the page reloads.
  }
}

export const rights: Schemas["RightsOverviewOut"] = {
  claimant: {
    claimant_type: "self_producing_creator",
    identity_status: "verified",
    identity_method: "video_call",
    account_control_token: null,
  },
  work_declaration: DECLARATIONS.work,
  partner_declaration: DECLARATIONS.partners,
  withdrawal_declaration: DECLARATIONS.withdrawal,
  authority_declaration: DECLARATIONS.authority,
  authority_withdrawal_declaration: DECLARATIONS.authorityWithdrawal,
  partners: [
    {
      id: publicId("partner", 1),
      kind: "platform",
      name: "Sample Video Platform",
      domain: "videos.sample-platform.example",
      path_prefix: "/novak-studio",
      note: "Our own channel — everything there is published by us.",
      created_at: "2026-09-18T10:20:00Z",
    },
    {
      id: publicId("partner", 2),
      kind: "distributor",
      name: "Example Courses",
      domain: "courses.example",
      path_prefix: null,
      note: "Licensed to host the colour grading tutorial.",
      created_at: "2026-09-20T08:05:00Z",
    },
  ],
  partners_none_affirmed_at: null,
  works: [
    work(ORIG.lisbon, {}),
    work(ORIG.ramen, {}),
    work(ORIG.acoustic, { asserted_right: "joint_right", asserted_right_detail: "Recorded together with two session musicians; agreement on file." }),
    work(ORIG.mobility, { rights_status: "self_declared", authority_declared_at: null, authority_declaration_version: null, notice_blockers: ["documents_not_complete"] }),
    work(ORIG.grading, { rights_status: "documents_complete", authority_declared_at: null, authority_declaration_version: null, notice_blockers: ["authority_not_declared"] }),
    work(ORIG.dolomites, {}),
    work(ORIG.sourdough, UNDECLARED),
  ],
  proofs: [
    {
      id: publicId("proof", 1),
      kind: "publication_record",
      original_id: ORIG.lisbon,
      filename: "lisbon-publication-record.pdf",
      content_type: "application/pdf",
      size_bytes: 184_320,
      uploaded_at: "2026-09-19T11:30:00Z",
    },
    {
      id: publicId("proof", 2),
      kind: "contract_excerpt",
      original_id: ORIG.acoustic,
      filename: "session-agreement-excerpt.pdf",
      content_type: "application/pdf",
      size_bytes: 96_256,
      uploaded_at: "2026-09-19T11:42:00Z",
    },
    {
      id: publicId("proof", 3),
      kind: "licence",
      original_id: ORIG.grading,
      filename: "courses-licence.pdf",
      content_type: "application/pdf",
      size_bytes: 248_832,
      uploaded_at: "2026-09-22T15:12:00Z",
    },
  ],
  proof_upload: { available: true, max_bytes: 10_485_760, content_types: ["application/pdf", "image/png", "image/jpeg"] },
};

// ---------------------------------------------------------------------------
// Notices
// ---------------------------------------------------------------------------
export const CONFIRMATION_TEXTS: Record<Schemas["RequiredConfirmationOut"]["kind"], Schemas["RequiredConfirmationOut"]> = {
  rights: {
    kind: "rights",
    version: "notice-rights-2026-09-17",
    en: "I confirm that I hold the relevant exclusive right in the identified work(s), or am currently authorised to enforce it on behalf of its holder.",
    de: "Ich bin Inhaber der maßgeblichen ausschließlichen Rechte an dem/den bezeichneten Werk(en) oder befugt, diese durchzusetzen.",
  },
  identity: {
    kind: "identity",
    version: "notice-identity-2026-09-17",
    en: "I have reviewed the listed findings and confirm that they concern my identified work(s).",
    de: "Ich habe die aufgeführten Fundstellen geprüft und bestätige, dass sie mein(e) bezeichnetes/n Werk(e) betreffen.",
  },
  authorisation: {
    kind: "authorisation",
    version: "notice-authorisation-2026-09-17",
    en: "I have checked my licences, authorised distributors/affiliates and other permissions known to me; I am not aware that the complained-of use is authorised.",
    de: "Ich habe meine Lizenzen, autorisierten Vertriebspartner/Affiliates und sonstigen mir bekannten Erlaubnisse geprüft; mir ist nicht bekannt, dass die beanstandete Nutzung erlaubt ist.",
  },
  exceptions: {
    kind: "exceptions",
    version: "notice-exceptions-2026-09-17",
    en: "I have considered, for each complained-of use, whether it may be permitted under applicable law — for a DMCA notice, including fair use — and nevertheless have the required good-faith belief that the use is unauthorised.",
    de: "Ich habe für jede beanstandete Nutzung geprüft, ob sie nach anwendbarem Recht zulässig sein kann - bei einer DMCA-Meldung einschließlich Fair Use - und bin gleichwohl in gutem Glauben davon überzeugt, dass die Nutzung unerlaubt ist.",
  },
  recipient: {
    kind: "recipient",
    version: "notice-recipient-2026-09-17",
    en: "I select the listed recipient and notice channel.",
    de: "Ich wähle den aufgeführten Empfänger und Meldekanal aus.",
  },
  accuracy: {
    kind: "accuracy",
    version: "notice-accuracy-2026-09-17",
    en: "I have reviewed the complete notice below and consider its statements accurate.",
    de: "Ich habe die vollständige Meldung unten geprüft und halte ihre Angaben für zutreffend.",
  },
  forwarding_warning: {
    kind: "forwarding_warning",
    version: "notice-forwarding-2026-09-17",
    en: "Before sending: this recipient may forward some or all of the notice to the website operator, account holder, hosting provider or a transparency/research service. The notice contains the identity and contact information shown above. I have reviewed those fields before submitting it.",
    de: "Vor dem Absenden: Dieser Empfänger kann die Meldung ganz oder teilweise an den Website-Betreiber, Kontoinhaber, Hosting-Anbieter oder einen Transparenz-/Forschungsdienst weiterleiten. Die oben angezeigten Identitäts- und Kontaktdaten sind Bestandteil der Meldung. Ich habe diese Daten vor dem Absenden geprüft.",
  },
};

const BASE_CONFIRMATIONS = ["rights", "identity", "authorisation", "exceptions", "recipient", "accuracy"] as const;

export function requiredConfirmations(forwards: boolean): Schemas["RequiredConfirmationOut"][] {
  const kinds: Schemas["RequiredConfirmationOut"]["kind"][] = [...BASE_CONFIRMATIONS];
  if (forwards) kinds.push("forwarding_warning");
  return kinds.map((kind) => CONFIRMATION_TEXTS[kind]);
}

interface Claimant {
  name: string;
  email: string;
  address: string;
  phone: string;
}

const DEMO_CLAIMANT: Claimant = {
  name: ORGANISATION.name,
  email: "rights@novak-studio.example",
  address: "Musterstraße 1, 12345 Beispielstadt",
  phone: "",
};

const SUBJECTS: Record<Notice["variant"], { en: string; de: string }> = {
  dsa_art16: {
    en: "Notice under Article 16 of Regulation (EU) 2022/2065",
    de: "Meldung nach Artikel 16 der Verordnung (EU) 2022/2065",
  },
  dmca_512c: { en: "Notification under 17 U.S.C. § 512(c)(3)", de: "Notification under 17 U.S.C. § 512(c)(3)" },
  dmca_512d: { en: "Notification under 17 U.S.C. § 512(d)", de: "Notification under 17 U.S.C. § 512(d)" },
  provider_policy: { en: "Copyright notice under your content policy", de: "Urheberrechtliche Meldung nach Ihren Inhaltsrichtlinien" },
};

const DSA_DECLARATION = {
  en: "I confirm that I have a bona fide belief that the information and allegations contained in this notice are accurate and complete. I consider the information identified above to be unlawful for the reasons stated in this notice.",
  de: "Ich bestätige, dass ich in gutem Glauben davon überzeugt bin, dass die in dieser Meldung enthaltenen Angaben und Behauptungen richtig und vollständig sind. Ich halte die oben bezeichneten Informationen aus den in dieser Meldung genannten Gründen für rechtswidrig.",
};

const DMCA_DECLARATION =
  "I have a good-faith belief that use of the material in the manner complained of is not authorised by the copyright owner, its agent, or the law. The information in this notification is accurate and, under penalty of perjury, I am the owner of the exclusive right allegedly infringed or am authorised to act on behalf of the owner of that exclusive right.";

export function noticeSubject(notice: Pick<Notice, "variant" | "language" | "work_title">): string {
  const language = notice.variant.startsWith("dmca") ? "en" : notice.language;
  return `${SUBJECTS[notice.variant][language]} — "${notice.work_title}"`;
}

/** The plain-text draft. No image, no hash, no confidence figure — a technical finding in words. */
export function renderNotice(
  notice: Pick<Notice, "variant" | "language" | "work_title" | "original_id" | "recipient_provider_name" | "recipient_hostname" | "recipient_email" | "recipient_url">,
  pages: readonly Pick<FindingRow, "url" | "confirmed_on" | "last_checked">[],
  claimant: Claimant,
  sourceUrl: string,
): string {
  const dmca = notice.variant.startsWith("dmca");
  const german = notice.language === "de" && !dmca;
  const recipient = notice.recipient_provider_name || notice.recipient_hostname;
  const contact = [claimant.email, claimant.address, claimant.phone].filter(Boolean).join(" · ");
  const seen = (value: string | null) => (value ? value.slice(0, 16).replace("T", " ") + " UTC" : "—");
  const locations = pages
    .map((page) =>
      german
        ? `- ${page.url} (geprüft am ${page.confirmed_on}, zuletzt abrufbar ${seen(page.last_checked)})`
        : `- ${page.url} (reviewed on ${page.confirmed_on}, last seen available ${seen(page.last_checked)})`,
    )
    .join("\n");
  if (german) {
    return [
      `An: ${recipient}${notice.recipient_email ? ` <${notice.recipient_email}>` : notice.recipient_url ? ` (${notice.recipient_url})` : ""}`,
      `Betreff: ${noticeSubject(notice)}`,
      "",
      "1. Meldende Partei",
      `${claimant.name}, Inhaber des ausschließlichen Rechts. Kontakt: ${contact}`,
      "",
      "2. Werk",
      `„${notice.work_title}“ (${notice.original_id})${sourceUrl ? `, rechtmäßig erhältlich unter ${sourceUrl}` : ""}.`,
      "",
      "3. Gemeldete Fundstellen",
      locations,
      "",
      "4. Technische Feststellung",
      "Das an den gemeldeten Fundstellen abrufbare Material wurde mit dem registrierten Referenzwerk verglichen. Das wahrnehmungsbasierte Vergleichssystem von Marceline hat übereinstimmende Inhalte erkannt, und eine prüfende Person hat den Vergleich geprüft und festgehalten, dass das an der gemeldeten Fundstelle abrufbare Material mit dem Referenzwerk übereinstimmt. Diese Angabe betrifft ausschließlich die inhaltliche Übereinstimmung; die meldende Partei bestätigt ihre Inhaberschaft oder Befugnis, das Fehlen einer Erlaubnis und die für diese Meldung geltenden Erklärungen gesondert. Bilder, Vorschaubilder oder Kopien des gemeldeten Materials sind nicht beigefügt.",
      "",
      "5. Erklärung",
      DSA_DECLARATION.de,
      "",
      claimant.name,
    ].join("\n");
  }
  return [
    `To: ${recipient}${notice.recipient_email ? ` <${notice.recipient_email}>` : notice.recipient_url ? ` (${notice.recipient_url})` : ""}`,
    `Subject: ${noticeSubject(notice)}`,
    "",
    "1. Notifying party",
    `${claimant.name}, the owner of the exclusive right. Contact: ${contact}`,
    "",
    "2. Work",
    `"${notice.work_title}" (${notice.original_id})${sourceUrl ? `, legitimately available at ${sourceUrl}` : ""}.`,
    "",
    "3. Reported locations",
    locations,
    "",
    "4. Technical finding",
    "The material available at the reported locations was compared with the registered reference work. Marceline's perceptual-comparison system identified matching content, and a human reviewer reviewed that comparison and recorded that the material available at the reported location matches the reference work. This statement concerns content identity only; the notifying party separately confirms its ownership or authority, the absence of permission and the declarations that apply to this notice. No images, thumbnails or copies of the reported material are attached.",
    "",
    "5. Declaration",
    dmca ? DMCA_DECLARATION : DSA_DECLARATION.en,
    "",
    claimant.name,
  ].join("\n");
}

export function noticeFinding(row: FindingRow, exceptionConfirmed: boolean): Schemas["NoticeFindingOut"] {
  return {
    page_id: row.page_id,
    url: row.url,
    availability: row.availability,
    extent: row.extent,
    requires_exception_review: row.requires_exception_review,
    exception_confirmed: exceptionConfirmed,
    included: true,
  };
}

interface DraftInput {
  originalId: string;
  pageIds: readonly string[];
  hostname: string;
  providerName: string;
  channelKind: ChannelKind;
  email: string;
  url: string;
  variant: Notice["variant"];
  language: Notice["language"];
  claimant: Claimant;
  sourceUrl: string;
  exceptionPageIds: readonly string[];
  forwards: boolean;
  createdAt: string;
}

export function draftNotice(id: string, input: DraftInput): Notice {
  const rows = findings.filter((row) => input.pageIds.includes(row.page_id));
  const head = {
    original_id: input.originalId,
    variant: input.variant,
    language: input.language,
    recipient_hostname: input.hostname,
    recipient_provider_name: input.providerName,
    recipient_email: input.email,
    recipient_url: input.url,
    work_title: originals.find((row) => row.id === input.originalId)?.title ?? "",
  };
  return {
    id,
    ...head,
    status: "draft",
    is_sandbox: false,
    channel_kind: input.channelKind,
    created_at: input.createdAt,
    approved_at: null,
    marked_sent_at: null,
    responded_at: null,
    outcome: null,
    removal_observed_at: null,
    withdrawn_at: null,
    withdrawal_reason: null,
    corrected_into: null,
    findings: rows.map((row) => noticeFinding(row, input.exceptionPageIds.includes(row.page_id))),
    required_confirmations: requiredConfirmations(input.forwards),
    confirmations: [],
    preview_text: renderNotice(head, rows, input.claimant, input.sourceUrl),
    approved_text: null,
  };
}

/** Tick every required confirmation, as the customer would, and freeze the text. */
export function approve(notice: Notice, at: string, language: Notice["language"] = notice.language): void {
  notice.status = "customer_approved";
  notice.approved_at = at;
  notice.approved_text = notice.preview_text;
  notice.confirmations = notice.required_confirmations.map((item) => ({
    kind: item.kind,
    version: item.version,
    language,
    text: item[language],
    confirmed_at: at,
  }));
}

function seeded(
  seed: number,
  status: "draft" | "approved" | "sent" | "responded",
  input: Omit<DraftInput, "claimant" | "exceptionPageIds" | "language" | "sourceUrl"> & Partial<Pick<DraftInput, "exceptionPageIds" | "language" | "sourceUrl">>,
  dates: { approved?: string; sent?: string; responded?: string; removalObserved?: string } = {},
): Notice {
  const notice = draftNotice(publicId("notice", seed), {
    claimant: DEMO_CLAIMANT,
    exceptionPageIds: [],
    language: "en",
    sourceUrl: "https://videos.sample-platform.example/novak-studio",
    ...input,
  });
  if (status === "draft") return notice;
  approve(notice, dates.approved ?? input.createdAt);
  if (status === "approved") return notice;
  notice.status = "marked_sent_by_customer";
  notice.marked_sent_at = dates.sent ?? notice.approved_at;
  if (status === "sent") return notice;
  notice.status = "provider_responded";
  notice.responded_at = dates.responded ?? notice.marked_sent_at;
  notice.outcome = "removed";
  notice.removal_observed_at = dates.removalObserved ?? null;
  return notice;
}

const clipvaultRecipient = {
  hostname: HOSTS.clipvault.hostname,
  providerName: "Example Hosting GmbH",
  channelKind: "dsa_art16",
  email: "abuse@example-hosting.example",
  url: "",
  variant: "dsa_art16",
  forwards: false,
} as const;

const streamdepotRecipient = {
  hostname: HOSTS.streamdepot.hostname,
  providerName: "Sample Cloud Inc.",
  channelKind: "dmca_512c",
  email: "copyright@sample-cloud.example",
  url: "",
  variant: "dmca_512c",
  forwards: true,
} as const;

/** Newest first, as the API lists them. */
export const notices: Notice[] = [
  seeded(4, "draft", { ...clipvaultRecipient, originalId: ORIG.ramen, pageIds: [publicId("page", PAGE.ramenClipvault)], createdAt: "2026-10-22T09:40:00Z" }),
  seeded(
    3,
    "approved",
    { ...streamdepotRecipient, originalId: ORIG.lisbon, pageIds: [publicId("page", PAGE.lisbonStreamdepot)], exceptionPageIds: [publicId("page", PAGE.lisbonStreamdepot)], createdAt: "2026-10-20T13:05:00Z" },
    { approved: "2026-10-20T13:18:00Z" },
  ),
  seeded(
    2,
    "sent",
    { ...clipvaultRecipient, originalId: ORIG.lisbon, pageIds: [publicId("page", PAGE.lisbonClipvault)], createdAt: "2026-10-12T08:30:00Z" },
    { approved: "2026-10-12T08:41:00Z", sent: "2026-10-12T08:55:00Z" },
  ),
  seeded(
    1,
    "responded",
    { ...streamdepotRecipient, originalId: ORIG.acoustic, pageIds: [publicId("page", PAGE.acousticStreamdepot)], createdAt: "2026-09-09T10:00:00Z" },
    { approved: "2026-09-09T10:12:00Z", sent: "2026-09-09T10:30:00Z", responded: "2026-09-15T16:00:00Z", removalObserved: "2026-09-16T09:15:00Z" },
  ),
];

// ---------------------------------------------------------------------------
// Invoices, uploads, session
// ---------------------------------------------------------------------------
function invoice(seed: number, number: string, month: string, overrides: Partial<Schemas["CustomerInvoiceOut"]> = {}): Schemas["CustomerInvoiceOut"] {
  const [year, monthNumber] = month.split("-").map(Number) as [number, number];
  const lastDay = new Date(Date.UTC(year, monthNumber, 0)).getUTCDate();
  return {
    id: publicId("inv", seed),
    number,
    kind: "invoice",
    status: "paid",
    issued_on: `${month}-01`,
    due_on: `${month}-15`,
    period_start: `${month}-01`,
    period_end: `${month}-${String(lastDay).padStart(2, "0")}`,
    currency: "EUR",
    net_cents: 4900,
    vat_cents: 931,
    gross_cents: 5831,
    vat_treatment: "domestic",
    paid_on: `${month}-09`,
    cancels_number: null,
    cancelled_by_number: null,
    ...overrides,
  };
}

export const invoices: Schemas["CustomerInvoiceOut"][] = [
  invoice(5, "2026-00041", "2026-11", { status: "open", paid_on: null }),
  invoice(4, "2026-00033", "2026-10"),
  invoice(3, "2026-00027", "2026-09"),
  invoice(2, "2026-00019", "2026-08", {
    kind: "cancellation",
    status: "issued",
    issued_on: "2026-08-05",
    due_on: null,
    net_cents: -9900,
    vat_cents: -1881,
    gross_cents: -11781,
    paid_on: null,
    cancels_number: "2026-00018",
  }),
  invoice(1, "2026-00018", "2026-08", {
    status: "cancelled",
    net_cents: 9900,
    vat_cents: 1881,
    gross_cents: 11781,
    paid_on: null,
    cancelled_by_number: "2026-00019",
  }),
];

export const submissions: Schemas["SubmissionOut"][] = [];

export const session = { authenticated: true, emailVerified: true };

// ---------------------------------------------------------------------------
// Showing the account is yours, and the creator's own data requests
// ---------------------------------------------------------------------------
function hoursFromNow(hours: number): string {
  return new Date(Date.now() + hours * 3_600_000).toISOString();
}

export const CODE_TTL_HOURS = 72;

export const verification: Schemas["VerificationStatusOut"] = {
  identity_status: "verified",
  method: "video_call",
  code_ttl_hours: CODE_TTL_HOURS,
  platforms: [
    { code: "own_website", label: "Own website", hosts: [], code_accepted: true },
    { code: "other_platform", label: "Another platform", hosts: [], code_accepted: true },
  ],
  accounts: [
    {
      id: publicId("chal", 2),
      platform: "other_platform",
      handle: "novakstudio.clips",
      profile_url: null,
      state: "pending",
      code: "MRC-7K2Q-9D4X",
      code_expires_at: hoursFromNow(41),
      method: null,
      decided_at: null,
      placed_at: null,
    },
    {
      id: publicId("chal", 1),
      platform: "own_website",
      handle: "novak-studio",
      profile_url: "https://novak-studio.example/about",
      state: "verified",
      code: null,
      code_expires_at: null,
      method: "video_call",
      decided_at: "2026-08-02T10:15:00Z",
      placed_at: "2026-08-01T16:40:00Z",
    },
  ],
};

export const DATA_REQUEST_DAYS = 30;

export const dataRequests: Schemas["DataRequestsOut"] = {
  contact_email: "privacy@marceline.example",
  deletion_phrase: "DELETE",
  requests: [
    {
      kind: "export",
      status: "done",
      requested_at: "2026-09-03T08:12:00Z",
      due_at: "2026-10-03T08:12:00Z",
      settled_at: "2026-09-10T14:30:00Z",
      open: false,
    },
  ],
};

// ---------------------------------------------------------------------------
// Views: what the API derives from the rows above
// ---------------------------------------------------------------------------
function isGone(row: FindingRow): boolean {
  return row.online === false;
}

export function findingsOf(originalId: string): FindingRow[] {
  return findings.filter((row) => row.original_id === originalId);
}

export function originalView(row: OriginalRow): Original {
  const own = findingsOf(row.id);
  const removed = own.filter(isGone).length;
  return {
    ...row,
    state: own.length === 0 ? "nothing_found" : removed === own.length ? "found_removed" : "found_online",
    counts: { confirmed: own.length, removed },
  };
}

function caseState(originalId: string): Finding["case_state"] {
  return findingsOf(originalId).every(isGone) ? "removed_watching" : "open";
}

export function findingView(row: FindingRow): Finding {
  // The two bookkeeping fields stay behind: the API sends neither.
  const { host: _host, requires_exception_review: _review, ...finding } = row;
  return { ...finding, case_state: caseState(row.original_id) };
}

function latest(values: readonly (string | null)[]): string | null {
  return values.reduce<string | null>((max, value) => (value !== null && (max === null || value > max) ? value : max), null);
}

function earliest(values: readonly (string | null)[]): string | null {
  return values.reduce<string | null>((min, value) => (value !== null && (min === null || value < min) ? value : min), null);
}

export function caseView(row: OriginalRow): Schemas["CustomerCaseOut"] | null {
  const own = findingsOf(row.id);
  if (own.length === 0) return null;
  const state = caseState(row.id);
  const reopenedAt = latest(own.map((finding) => finding.reappeared_at));
  return {
    original_id: row.id,
    title: row.title,
    state,
    opened_on: earliest(own.map((finding) => finding.confirmed_on)) ?? row.created_at.slice(0, 10),
    removed_at: state === "removed_watching" ? latest(own.map((finding) => finding.removed_at)) : null,
    reopened_on: reopenedAt ? reopenedAt.slice(0, 10) : null,
    reopen_count: own.filter((finding) => finding.reappeared_at !== null).length,
    counts: originalView(row).counts,
  };
}

function step(code: Schemas["CaseStepOut"]["step"], at: string | null, on: string | null = null): Schemas["CaseStepOut"] {
  return { step: code, status: at !== null || on !== null ? "done" : "pending", at, on, reason: null, blocked_by_issue: null };
}

export function caseDetail(row: OriginalRow): Schemas["CustomerCaseDetailOut"] {
  const own = findingsOf(row.id);
  const drafts = notices.filter((notice) => notice.original_id === row.id && notice.status !== "withdrawn");
  return {
    original: originalView(row),
    case: caseView(row),
    findings: own.map(findingView),
    timeline:
      own.length === 0
        ? []
        : [
            step("confirmed", null, earliest(own.map((finding) => finding.confirmed_on))),
            step("notice_prepared", earliest(drafts.map((notice) => notice.created_at))),
            step("notice_sent", earliest(drafts.map((notice) => notice.marked_sent_at))),
            step("notice_acknowledged", earliest(drafts.map((notice) => notice.responded_at))),
            step("removed", latest(own.map((finding) => finding.removed_at))),
            step("rechecked", latest(own.map((finding) => finding.last_checked))),
          ],
    not_available: [
      { topic: "notice_delivery", reason: "legal_review_pending", blocked_by_issue: 46 },
      { topic: "reupload_watch_coverage", reason: "capability_not_built", blocked_by_issue: null },
    ],
  };
}

export function removalsView(): Schemas["CustomerRemovalOut"][] {
  return findings
    .filter((row) => row.removed_at !== null)
    .map((row) => {
      const state = caseState(row.original_id);
      return {
        original_id: row.original_id,
        title: originals.find((original) => original.id === row.original_id)?.title ?? "",
        page_id: row.page_id,
        site: row.site,
        url: row.url,
        removed_at: row.removed_at ?? "",
        confirmed_on: row.confirmed_on,
        reappeared_at: row.reappeared_at,
        last_checked: row.last_checked,
        state: row.reappeared_at === null ? ("removed" as const) : ("reopened" as const),
        case_state: state,
        handled: row.reappeared_at === null && state === "removed_watching",
      };
    })
    .sort((a, b) => b.removed_at.localeCompare(a.removed_at));
}

function channel(kind: ChannelKind, email: string | null, forwards: boolean | null): Schemas["NoticeChannelOut"] {
  return {
    kind,
    support: "primary",
    email,
    url: null,
    source: kind === "abuse_email" ? "rdap" : "directory",
    checked_at: "2026-10-01T00:00:00Z",
    recheck_due: false,
    channel_id: null,
    forwards_to_site_operator: forwards,
    publishes_to_transparency_db: forwards,
  };
}

/** Who a notice about a page on this host could go to, and through which channel. */
function providersFor(host: HostKey): Schemas["NoticeProviderOut"][] {
  const page = HOSTS[host];
  if (host === "clipvault") {
    return [
      {
        hostname: page.hostname,
        slot: "network",
        provider_name: "Example Hosting GmbH",
        nexus: "eu_facing",
        function: "origin_host",
        channels: [channel("dsa_art16", "abuse@example-hosting.example", false), channel("abuse_email", "abuse@example-hosting.example", null)],
        suggested_channel_kind: "dsa_art16",
      },
    ];
  }
  if (host === "streamdepot") {
    return [
      {
        hostname: page.hostname,
        slot: "network",
        provider_name: "Sample Cloud Inc.",
        nexus: "us_nexus",
        function: "origin_host",
        channels: [channel("dmca_512c", "copyright@sample-cloud.example", true)],
        suggested_channel_kind: "dmca_512c",
      },
    ];
  }
  if (host === "reuploads") {
    return [
      {
        hostname: page.hostname,
        slot: "registrar",
        provider_name: "Demo Domains B.V.",
        nexus: "eu_facing",
        function: "registrar",
        channels: [channel("abuse_email", "abuse@demo-domains.example", null)],
        suggested_channel_kind: "abuse_email",
      },
    ];
  }
  return [];
}

export function candidatesView(row: OriginalRow): Schemas["NoticeCandidatesOut"] {
  const inOpenNotice = new Set(
    notices
      .filter((notice) => notice.status !== "withdrawn" && notice.status !== "corrected")
      .flatMap((notice) => notice.findings.filter((finding) => finding.included).map((finding) => finding.page_id)),
  );
  const workBlockers = rights.works.find((item) => item.original_id === row.id)?.notice_blockers ?? [];
  return {
    original_id: row.id,
    title: row.title,
    findings: findingsOf(row.id)
      .filter((finding) => !inOpenNotice.has(finding.page_id))
      .map((finding) => {
        const providers = providersFor(finding.host);
        const blockers: string[] = [...workBlockers];
        if (isGone(finding)) blockers.push("page_not_available");
        else if (providers.length === 0) blockers.push("no_supported_channel");
        if (blockers.length === 0 && finding.requires_exception_review) blockers.push("exception_review_required");
        return {
          page_id: finding.page_id,
          site: finding.site,
          url: finding.url,
          availability: finding.availability,
          last_checked: finding.last_checked,
          extent: finding.extent,
          requires_exception_review: finding.requires_exception_review,
          blockers,
          providers,
        };
      }),
  };
}

export function forwardsNotices(hostname: string, kind: ChannelKind): boolean {
  const host = (Object.keys(HOSTS) as HostKey[]).find((key) => HOSTS[key].hostname === hostname);
  if (!host) return false;
  return providersFor(host).some((provider) =>
    provider.channels.some((item) => item.kind === kind && item.forwards_to_site_operator === true),
  );
}

for (const row of addedRows()) {
  taken.add(row.id);
  addOriginal(row);
}
