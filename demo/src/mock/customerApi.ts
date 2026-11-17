/**
 * The customer API (`/api/v1/app/…`) answered from `state.ts`. Reads are views over the rows;
 * writes change them in memory, so a draft you approve stays approved until the page reloads.
 */
import { say } from "./notify";
import { type MockReply, type MockRequest, ok, refuse } from "./reply";
import {
  type ChannelKind,
  type Notice,
  ORGANISATION,
  type RightsWork,
  type Schemas,
  USERNAME,
  approve,
  candidatesView,
  caseDetail,
  caseView,
  draftNotice,
  findingView,
  findings,
  findingsOf,
  forwardsNotices,
  invoices,
  nextId,
  notices,
  nowIso,
  originalView,
  originals,
  removalsView,
  rights,
  session,
  submissions,
  undeclaredWork,
} from "./state";

type Params = Record<string, string>;
type Handler = (request: MockRequest, params: Params) => MockReply;

const routes: { method: string; pattern: RegExp; keys: string[]; handler: Handler }[] = [];

/** Register `METHOD /app/path/{param}`; a `{param}` matches one path segment. */
function on(method: string, path: string, handler: Handler): void {
  const keys: string[] = [];
  const pattern = path.replace(/\{(\w+)\}/g, (_match, key: string) => {
    keys.push(key);
    return "([^/]+)";
  });
  routes.push({ method, pattern: new RegExp(`^/api/v1/app${pattern}/?$`), keys, handler });
}

export function handleCustomerApi(request: MockRequest): MockReply | null {
  if (!request.path.startsWith("/api/v1/app/")) return null;
  for (const route of routes) {
    if (route.method !== request.method) continue;
    const match = route.pattern.exec(request.path);
    if (!match) continue;
    const params: Params = {};
    route.keys.forEach((key, index) => {
      params[key] = decodeURIComponent(match[index + 1] ?? "");
    });
    return route.handler(request, params);
  }
  return null;
}

function record(body: unknown): Record<string, unknown> {
  return typeof body === "object" && body !== null && !(body instanceof FormData) ? (body as Record<string, unknown>) : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function saved(reply: MockReply): MockReply {
  say("saved");
  return reply;
}

// --- Session ---------------------------------------------------------------
function account(): Schemas["AccountOut"] {
  return {
    username: USERNAME,
    organisation: ORGANISATION,
    monitored_sources: "50_plus",
    can_add_originals: true,
    can_upload_media: false,
    can_export_data: true,
    can_verify_account: false,
    can_request_data: false,
    email_verified: true,
    can_verify_email: false,
  };
}

function sessionView(): Schemas["CustomerSessionOut"] {
  return session.authenticated ? { authenticated: true, username: USERNAME } : { authenticated: false, username: null };
}

on("GET", "/auth/session", () => ok(sessionView()));
on("GET", "/me", () => (session.authenticated ? ok(account()) : refuse(401, "not_authenticated")));
on("POST", "/auth/login", () => {
  session.authenticated = true;
  say("signedIn");
  return ok(account());
});
on("POST", "/auth/logout", () => {
  session.authenticated = false;
  say("signedOut");
  return ok(sessionView());
});

// --- Originals, findings, records -------------------------------------------
on("GET", "/originals", () => ok(originals.map(originalView)));
on("GET", "/originals/{id}", (_request, { id }) => {
  const row = originals.find((original) => original.id === id);
  if (!row) return refuse(404, "Original not found");
  const detail: Schemas["CustomerOriginalDetailOut"] = {
    original: originalView(row),
    findings: findingsOf(row.id).map(({ page_id, site, url, availability, last_checked, evidence, extent }) => ({
      page_id,
      site,
      url,
      availability,
      last_checked,
      evidence,
      extent,
    })),
  };
  return ok(detail);
});
on("GET", "/findings", () => ok(findings.map(findingView)));
on("GET", "/cases", () => ok(originals.map(caseView).filter((item) => item !== null)));
on("GET", "/cases/{id}", (_request, { id }) => {
  const row = originals.find((original) => original.id === id);
  return row ? ok(caseDetail(row)) : refuse(404, "Case not found");
});
on("GET", "/removals", () => ok(removalsView()));
on("GET", "/invoices", () => ok(invoices));

// --- Add an original ---------------------------------------------------------
on("GET", "/fingerprints/submissions", () => ok(submissions));
on("POST", "/fingerprints/client-events", () => ok({ recorded: true }));
on("POST", "/fingerprints", (request) => {
  const fingerprint = record(record(request.body).fingerprint);
  const title = text(fingerprint.title).trim() || "Untitled original";
  const duration = typeof fingerprint.duration_s === "number" ? fingerprint.duration_s : null;
  const row = {
    id: nextId("orig"),
    title,
    kind: fingerprint.kind === "photos" ? ("photos" as const) : ("video" as const),
    duration_s: fingerprint.kind === "photos" ? null : duration,
    created_at: nowIso(),
  };
  originals.unshift(row);
  rights.works.push(undeclaredWork(row.id, title));
  say("added");
  const added: Schemas["AddOriginalOut"] = { original: originalView(row), created: true };
  return { status: 201, body: added };
});

// --- Rights ------------------------------------------------------------------
function withWork(id: string | undefined, change: (work: RightsWork) => void): MockReply {
  const item = rights.works.find((work) => work.original_id === id);
  if (!item) return refuse(404, "not_found");
  change(item);
  return saved(ok(rights));
}

on("GET", "/rights", () => ok(rights));
on("PUT", "/rights/claimant", (request) => {
  const type = record(request.body).claimant_type as Schemas["ClaimantTypeIn"]["claimant_type"];
  rights.claimant.claimant_type = type;
  return saved(ok(rights));
});
on("POST", "/rights/claimant/token", () => {
  rights.claimant.account_control_token = `marceline-demo-${nextId("t").slice(2, 10)}`;
  return saved(ok(rights));
});
on("POST", "/rights/works/{id}/declaration", (request, { id }) =>
  withWork(id, (work) => {
    const body = record(request.body) as Partial<Schemas["WorkDeclarationIn"]>;
    work.rights_status = "self_declared";
    work.capacity = body.capacity ?? "owner";
    work.asserted_right = body.asserted_right ?? "copyright";
    work.underlying_owner_name = body.underlying_owner_name || null;
    work.asserted_right_detail = body.asserted_right_detail ?? "";
    work.declared_at = nowIso();
    work.declaration_version = rights.work_declaration.version;
    work.notice_blockers = ["documents_not_complete"];
  }),
);
on("POST", "/rights/works/{id}/withdrawal", (_request, { id }) =>
  withWork(id, (work) => {
    Object.assign(work, undeclaredWork(work.original_id, work.title));
  }),
);
on("POST", "/rights/works/{id}/authority", (_request, { id }) =>
  withWork(id, (work) => {
    work.rights_status = "authority_declared";
    work.authority_declared_at = nowIso();
    work.authority_declaration_version = rights.authority_declaration.version;
    work.notice_blockers = [];
  }),
);
on("POST", "/rights/works/{id}/authority/withdrawal", (_request, { id }) =>
  withWork(id, (work) => {
    work.rights_status = "documents_complete";
    work.authority_declared_at = null;
    work.authority_declaration_version = null;
    work.notice_blockers = ["authority_not_declared"];
  }),
);
on("POST", "/rights/partners", (request) => {
  const body = record(request.body) as Partial<Schemas["PartnerIn"]>;
  rights.partners.push({
    id: nextId("partner"),
    kind: body.kind ?? "domain",
    name: text(body.name) || "—",
    domain: body.domain || null,
    path_prefix: body.path_prefix || null,
    note: body.note ?? "",
    created_at: nowIso(),
  });
  rights.partners_none_affirmed_at = null;
  return saved(ok(rights));
});
on("POST", "/rights/partners/none", () => {
  if (rights.partners.length > 0) return refuse(409, "partners_exist");
  rights.partners_none_affirmed_at = nowIso();
  return saved(ok(rights));
});
on("DELETE", "/rights/partners/{id}", (_request, { id }) => {
  rights.partners = rights.partners.filter((partner) => partner.id !== id);
  return saved(ok(rights));
});
on("POST", "/rights/proofs", (request) => {
  // Only the file's name, type and size are read; its bytes go nowhere and are not kept.
  const form = request.body instanceof FormData ? request.body : new FormData();
  const file = [...form.values()].find((value): value is File => value instanceof File);
  const kind = text(form.get("kind")) as Schemas["ProofOut"]["kind"];
  const originalId = text(form.get("original_id"));
  rights.proofs.push({
    id: nextId("proof"),
    kind: kind || "other",
    original_id: rights.works.some((work) => work.original_id === originalId) ? originalId : null,
    filename: file?.name ?? "document.pdf",
    content_type: file?.type || "application/pdf",
    size_bytes: file?.size ?? 0,
    uploaded_at: nowIso(),
  });
  return saved(ok(rights));
});

// --- Notice drafts -----------------------------------------------------------
function withNotice(id: string | undefined, change: (notice: Notice) => MockReply | void): MockReply {
  const notice = notices.find((item) => item.id === id);
  if (!notice) return refuse(404, "notice_not_found");
  return change(notice) ?? saved(ok(notice));
}

on("GET", "/notices", () => ok(notices));
on("GET", "/notices/submissions", () => ok([]));
on("GET", "/notices/candidates/{id}", (_request, { id }) => {
  const row = originals.find((original) => original.id === id);
  return row ? ok(candidatesView(row)) : refuse(404, "not_found");
});
on("GET", "/notices/{id}", (_request, { id }) => withNotice(id, (notice) => ok(notice)));
on("POST", "/notices", (request) => {
  const body = record(request.body) as Partial<Schemas["CreateNoticeIn"]>;
  const pageIds = body.page_ids ?? [];
  if (pageIds.length === 0) return refuse(409, "no_findings_selected");
  if (!body.original_id || !originals.some((row) => row.id === body.original_id)) return refuse(404, "not_found");
  const recipient = body.recipient;
  const channelKind: ChannelKind = recipient?.channel_kind ?? "abuse_email";
  const hostname = recipient?.hostname ?? "";
  const notice = draftNotice(nextId("notice"), {
    originalId: body.original_id,
    pageIds,
    hostname,
    providerName: recipient?.provider_name ?? "",
    channelKind,
    email: recipient?.email ?? "",
    url: recipient?.url ?? "",
    variant: body.variant ?? "provider_policy",
    language: body.language ?? "en",
    claimant: {
      name: text(body.claimant?.name),
      email: text(body.claimant?.email),
      address: text(body.claimant?.address),
      phone: text(body.claimant?.phone),
    },
    sourceUrl: text(body.legitimate_source_url),
    exceptionPageIds: body.exception_confirmed_page_ids ?? [],
    forwards: forwardsNotices(hostname, channelKind),
    createdAt: nowIso(),
  });
  notices.unshift(notice);
  return saved(ok(notice));
});
on("POST", "/notices/{id}/approve", (request, { id }) =>
  withNotice(id, (notice) => {
    if (notice.status !== "draft") return refuse(409, "not_draft");
    const sent = Array.isArray(request.body) ? (request.body as Schemas["ConfirmationIn"][]) : [];
    const complete = notice.required_confirmations.every((required) =>
      sent.some((item) => item.kind === required.kind && item.confirmed),
    );
    if (!complete) return refuse(409, "confirmation_incomplete");
    approve(notice, nowIso(), sent[0]?.language ?? notice.language);
  }),
);
on("POST", "/notices/{id}/mark-sent", (_request, { id }) =>
  withNotice(id, (notice) => {
    if (notice.status !== "customer_approved") return refuse(409, "not_approved");
    notice.status = "marked_sent_by_customer";
    notice.marked_sent_at = nowIso();
  }),
);
on("POST", "/notices/sandbox-send/{id}", (_request, { id }) => withNotice(id, () => refuse(409, "not_sandbox")));
on("POST", "/notices/{id}/response", (request, { id }) =>
  withNotice(id, (notice) => {
    if (notice.status !== "marked_sent_by_customer") return refuse(409, "not_sent");
    const body = record(request.body) as Partial<Schemas["ResponseIn"]>;
    notice.status = "provider_responded";
    notice.outcome = body.outcome ?? "no_response";
    notice.responded_at = body.responded_at || nowIso();
  }),
);
on("POST", "/notices/{id}/withdraw", (request, { id }) =>
  withNotice(id, (notice) => {
    if (notice.status === "withdrawn" || notice.status === "corrected") return refuse(409, "already_closed");
    notice.status = "withdrawn";
    notice.withdrawn_at = nowIso();
    notice.withdrawal_reason = text(record(request.body).reason) || "other";
  }),
);
on("POST", "/notices/{id}/correct", (_request, { id }) =>
  withNotice(id, (notice) => {
    if (notice.status === "withdrawn" || notice.status === "corrected") return refuse(409, "already_closed");
    // A correction closes this draft and opens a fresh one with the same contents to edit.
    const successor: Notice = {
      ...notice,
      id: nextId("notice"),
      status: "draft",
      created_at: nowIso(),
      approved_at: null,
      marked_sent_at: null,
      responded_at: null,
      outcome: null,
      removal_observed_at: null,
      confirmations: [],
      approved_text: null,
    };
    notice.status = "corrected";
    notice.corrected_into = successor.id;
    notices.unshift(successor);
  }),
);
