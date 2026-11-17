/**
 * The customer API (`/api/v1/app/…`) answered from `state.ts`. Reads are views over the rows;
 * writes change them in memory, so a draft you approve stays approved until the page reloads.
 */
import { dataExport, invoicePdf, noticeFile } from "./files";
import { say } from "./notify";
import { type MockReply, type MockRequest, download, ok, refuse } from "./reply";
import {
  type ChannelKind,
  type Notice,
  ORGANISATION,
  addOriginal,
  rememberAdded,
  type RightsWork,
  type Schemas,
  USERNAME,
  approve,
  CODE_TTL_HOURS,
  DATA_REQUEST_DAYS,
  candidatesView,
  caseDetail,
  caseView,
  draftNotice,
  findingView,
  dataRequests,
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
  verification,
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

/** Something the demo has no answer for: a read is "not found", a write is declined, both politely. */
function unknown(request: MockRequest): MockReply {
  if (request.method === "GET") return refuse(404, "not_found");
  say("notInDemo");
  return refuse(503, "demo_unavailable");
}

/** The answer Django would give, from memory. Never throws: a failing handler is "not available". */
export function answerCustomerApi(request: MockRequest): MockReply {
  try {
    return handleCustomerApi(request) ?? unknown(request);
  } catch (error) {
    console.warn("[demo] mock API failed for", request.method, request.path, error);
    return unknown(request);
  }
}

function handleCustomerApi(request: MockRequest): MockReply | null {
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
    // Sending the file itself stays off, as on the public host: an original is fingerprinted in the
    // visitor's own browser, which is also what keeps the banner's promise true.
    can_upload_media: false,
    can_export_data: true,
    can_verify_account: true,
    can_request_data: true,
    email_verified: session.emailVerified,
    can_verify_email: true,
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


// --- The mailed account actions: no mail exists here, so every link "works" --------------------
on("POST", "/auth/password-reset", () => {
  say("noMail");
  return ok({});
});
on("GET", "/auth/password-reset/{token}", () => ok({ email: USERNAME, password_min_length: 12 }));
on("POST", "/auth/password-reset/{token}", () => {
  say("passwordKept");
  return ok({ pending: false });
});
on("POST", "/auth/verify-email", () => {
  say("noMail");
  return ok({});
});
on("GET", "/auth/verify-email/{token}", () => ok({ email: USERNAME }));
on("POST", "/auth/verify-email/{token}", () => {
  session.emailVerified = true;
  return saved(ok({}));
});

// --- Downloads -----------------------------------------------------------------
function file(found: ReturnType<typeof invoicePdf>): MockReply {
  if (!found) return refuse(404, "not_found");
  say("download");
  return download(found);
}
on("GET", "/export", () => file(dataExport()));
on("GET", "/invoices/{id}/pdf", (_request, { id }) => file(invoicePdf(id)));
on("GET", "/notices/{id}/export/{format}", (_request, { id, format }) => file(noticeFile(id, format)));

// --- Showing the account is yours -------------------------------------------------
on("GET", "/verification", () => ok(verification));
on("POST", "/verification/accounts", (request) => {
  const body = record(request.body);
  const platform = verification.platforms.find((entry) => entry.code === body.platform);
  if (!platform) return refuse(400, "unknown_platform");
  const handle = text(body.handle).trim();
  if (!/^[A-Za-z0-9._-]{1,64}$/.test(handle)) return refuse(400, "invalid_handle");
  const letters = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const block = () => Array.from({ length: 4 }, () => letters[Math.floor(Math.random() * letters.length)]).join("");
  verification.accounts.unshift({
    id: nextId("chal"),
    platform: platform.code,
    handle,
    profile_url: text(body.profile_url).trim() || null,
    state: "pending",
    code: `MRC-${block()}-${block()}`,
    code_expires_at: new Date(Date.now() + CODE_TTL_HOURS * 3_600_000).toISOString(),
    method: null,
    decided_at: null,
    placed_at: null,
  });
  return saved(ok(verification));
});
on("POST", "/verification/accounts/{id}/placed", (_request, { id }) => {
  const claim = verification.accounts.find((account) => account.id === id);
  if (!claim) return refuse(404, "not_found");
  if (claim.state !== "pending") return refuse(409, "not_open");
  claim.state = "placed";
  claim.placed_at = nowIso();
  return saved(ok(verification));
});

// --- The creator's own data requests ---------------------------------------------
function askFor(kind: "export" | "deletion"): MockReply {
  if (dataRequests.requests.some((item) => item.kind === kind && item.open)) return refuse(409, "already_open");
  dataRequests.requests.unshift({
    kind,
    status: "requested",
    requested_at: nowIso(),
    due_at: new Date(Date.now() + DATA_REQUEST_DAYS * 86_400_000).toISOString(),
    settled_at: null,
    open: true,
  });
  return saved(ok(dataRequests));
}
function takeBack(kind: "export" | "deletion"): MockReply {
  const open = dataRequests.requests.find((item) => item.kind === kind && item.open);
  if (!open) return refuse(409, "not_open");
  open.status = "withdrawn";
  open.open = false;
  open.settled_at = nowIso();
  return saved(ok(dataRequests));
}
on("GET", "/data-requests", () => ok(dataRequests));
on("POST", "/data-requests/export", () => askFor("export"));
on("POST", "/data-requests/export/withdraw", () => takeBack("export"));
on("POST", "/data-requests/deletion", (request) =>
  text(record(request.body).confirm).trim() === dataRequests.deletion_phrase
    ? askFor("deletion")
    : refuse(400, "confirmation_mismatch"),
);
on("POST", "/data-requests/deletion/withdraw", () => takeBack("deletion"));

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
  addOriginal(row);
  rememberAdded(row);
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
/** A bare lowercase host from a host or URL, or null: the rule the real API stores partners by. */
function partnerDomain(value: string): string | null {
  let host: string;
  try {
    host = new URL(value.includes("//") ? value : `https://${value}`).hostname;
  } catch {
    return null;
  }
  host = host.replace(/\.$/, "").replace(/^www\./, "").toLowerCase();
  const labels = host.split(".");
  return labels.length >= 2 && labels.every((label) => /^[a-z0-9-]{1,63}$/.test(label)) ? host : null;
}

on("POST", "/rights/partners", (request) => {
  const body = record(request.body) as Partial<Schemas["PartnerIn"]>;
  const kind = body.kind ?? "domain";
  const name = text(body.name).trim();
  if (!name) return refuse(409, "partner_name_required");
  const given = text(body.domain).trim();
  const domain = given ? partnerDomain(given) : "";
  if (domain === null) return refuse(409, "invalid_domain");
  const prefix = text(body.path_prefix).trim().replace(/^\/+|\/+$/g, "");
  if (!domain && (kind === "domain" || kind === "platform" || prefix)) return refuse(409, "partner_domain_required");
  rights.partners.push({
    id: nextId("partner"),
    kind,
    name,
    domain: domain || null,
    path_prefix: prefix ? `/${prefix}` : null,
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
