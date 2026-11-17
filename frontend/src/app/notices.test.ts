import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { de } from "../i18n/de";
import { en } from "../i18n/en";
import type { Notice, NoticeCandidateFinding, NoticeFinding } from "./queries";
import {
  EXPORT_FORMATS,
  NOTICE_WRITE_KINDS,
  SUBMISSION_CODES,
  blockerLine,
  canApprove,
  canExport,
  canMarkSent,
  canRecordResponse,
  canSandboxSend,
  codeKey,
  declarationLanguage,
  errorKey,
  includedFindingsCount,
  isEligible,
  isOpen,
  isPendingWrite,
  isSelectable,
  isUnsettled,
  isUsableChannel,
  mailtoLink,
  noticeExportPath,
  noticeTone,
  onlyNeedsExceptionTick,
  submissionKindKey,
  submissionMessage,
  variantForChannelKind,
} from "./notices";

function finding(overrides: Partial<NoticeCandidateFinding> = {}): NoticeCandidateFinding {
  return {
    page_id: "page_" + "1".repeat(32),
    site: "site01.example",
    url: "https://site01.example/watch/1",
    availability: "playable",
    last_checked: "2026-09-17T10:00:00Z",
    extent: "full_copy",
    requires_exception_review: false,
    blockers: [],
    providers: [],
    ...overrides,
  };
}

function noticeFinding(overrides: Partial<NoticeFinding> = {}): NoticeFinding {
  return {
    page_id: "page_" + "1".repeat(32),
    url: "https://site01.example/watch/1",
    availability: "playable",
    extent: "full_copy",
    requires_exception_review: false,
    exception_confirmed: false,
    included: true,
    ...overrides,
  };
}

function notice(overrides: Partial<Notice> = {}): Notice {
  return {
    id: "notice_" + "1".repeat(32),
    original_id: "orig_" + "1".repeat(32),
    status: "draft",
    is_sandbox: false,
    variant: "dsa_art16",
    language: "en",
    recipient_hostname: "site01.example",
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
    removal_observed_at: null,
    withdrawn_at: null,
    withdrawal_reason: null,
    corrected_into: null,
    findings: [],
    required_confirmations: [],
    confirmations: [],
    preview_text: "preview",
    approved_text: null,
    ...overrides,
  };
}

describe("isEligible / isSelectable / onlyNeedsExceptionTick", () => {
  it("a finding with no blockers is eligible and selectable", () => {
    expect(isEligible(finding())).toBe(true);
    expect(isSelectable(finding(), new Set())).toBe(true);
  });

  it("a finding blocked on anything else is neither eligible nor selectable", () => {
    const blocked = finding({ blockers: ["page_not_available"] });
    expect(isEligible(blocked)).toBe(false);
    expect(onlyNeedsExceptionTick(blocked)).toBe(false);
    expect(isSelectable(blocked, new Set([blocked.page_id]))).toBe(false);
  });

  it("a finding blocked only on the exception tick becomes selectable once ticked", () => {
    const excerpt = finding({ blockers: ["exception_review_required"] });
    expect(isEligible(excerpt)).toBe(false);
    expect(onlyNeedsExceptionTick(excerpt)).toBe(true);
    expect(isSelectable(excerpt, new Set())).toBe(false);
    expect(isSelectable(excerpt, new Set([excerpt.page_id]))).toBe(true);
  });

  it("a finding blocked on the exception tick and something else stays unselectable however it is ticked", () => {
    const both = finding({ blockers: ["exception_review_required", "page_not_available"] });
    expect(onlyNeedsExceptionTick(both)).toBe(false);
    expect(isSelectable(both, new Set([both.page_id]))).toBe(false);
  });
});

describe("isUsableChannel / variantForChannelKind", () => {
  it("only primary and delisting_draft channels are usable in v1", () => {
    expect(isUsableChannel({ support: "primary" } as never)).toBe(true);
    expect(isUsableChannel({ support: "delisting_draft" } as never)).toBe(true);
    expect(isUsableChannel({ support: "forwarding_only" } as never)).toBe(false);
    expect(isUsableChannel({ support: "additional_contact_only" } as never)).toBe(false);
    expect(isUsableChannel({ support: "needs_classification" } as never)).toBe(false);
    expect(isUsableChannel({ support: "not_supported" } as never)).toBe(false);
  });

  it("maps each channel kind to its renderer variant", () => {
    expect(variantForChannelKind("dsa_art16")).toBe("dsa_art16");
    expect(variantForChannelKind("dmca_512c")).toBe("dmca_512c");
    expect(variantForChannelKind("dmca_512d")).toBe("dmca_512d");
    expect(variantForChannelKind("provider_copyright_policy")).toBe("provider_policy");
    expect(variantForChannelKind("provider_copyright_policy_dmca_style")).toBe("provider_policy");
    expect(variantForChannelKind("abuse_email")).toBe("provider_policy");
    expect(variantForChannelKind("web_form")).toBe("provider_policy");
  });
});

describe("status helpers", () => {
  it("open statuses are anything but withdrawn or corrected", () => {
    expect(isOpen("draft")).toBe(true);
    expect(isOpen("customer_approved")).toBe(true);
    expect(isOpen("marked_sent_by_customer")).toBe(true);
    expect(isOpen("provider_responded")).toBe(true);
    expect(isOpen("withdrawn")).toBe(false);
    expect(isOpen("corrected")).toBe(false);
  });

  it("each action is offered for exactly its status", () => {
    expect(canApprove(notice({ status: "draft" }))).toBe(true);
    expect(canApprove(notice({ status: "customer_approved" }))).toBe(false);

    expect(canMarkSent(notice({ status: "customer_approved", is_sandbox: false }))).toBe(true);
    expect(canMarkSent(notice({ status: "customer_approved", is_sandbox: true }))).toBe(false);
    expect(canMarkSent(notice({ status: "draft", is_sandbox: false }))).toBe(false);

    expect(canSandboxSend(notice({ status: "customer_approved", is_sandbox: true }))).toBe(true);
    expect(canSandboxSend(notice({ status: "customer_approved", is_sandbox: false }))).toBe(false);

    expect(canRecordResponse(notice({ status: "marked_sent_by_customer" }))).toBe(true);
    expect(canRecordResponse(notice({ status: "customer_approved" }))).toBe(false);
  });
});

describe("mailtoLink", () => {
  it("builds a mailto link with the approved text once approved and an email is known", () => {
    const approved = notice({ approved_text: "Dear Sir or Madam,\n\nSee attached.", recipient_email: "abuse@hosting.example" });
    const link = mailtoLink(approved);
    expect(link).toMatch(/^mailto:abuse@hosting\.example\?subject=.+&body=.+$/);
  });

  it("is null before approval, and null without a recipient email (a web-form channel)", () => {
    expect(mailtoLink(notice({ approved_text: null }))).toBeNull();
    expect(mailtoLink(notice({ approved_text: "text", recipient_email: "" }))).toBeNull();
  });
});

describe("declarationLanguage", () => {
  it("follows the browser/UI language, German only for de*", () => {
    expect(declarationLanguage("de")).toBe("de");
    expect(declarationLanguage("de-DE")).toBe("de");
    expect(declarationLanguage("en")).toBe("en");
    expect(declarationLanguage("fr")).toBe("en");
  });
});

describe("includedFindingsCount", () => {
  it("counts only findings the customer has not dropped", () => {
    const drafted = notice({
      findings: [
        noticeFinding({ page_id: "page_" + "1".repeat(32), included: true }),
        noticeFinding({ page_id: "page_" + "2".repeat(32), included: false }),
        noticeFinding({ page_id: "page_" + "3".repeat(32), included: true }),
      ],
    });
    expect(includedFindingsCount(drafted)).toBe(2);
  });

  it("is zero for a notice with no findings", () => {
    expect(includedFindingsCount(notice({ findings: [] }))).toBe(0);
  });
});

describe("noticeTone", () => {
  it("is neutral before anything has been sent, and after withdrawal or correction", () => {
    expect(noticeTone(notice({ status: "draft" }))).toBe("neutral");
    expect(noticeTone(notice({ status: "customer_approved" }))).toBe("neutral");
    expect(noticeTone(notice({ status: "withdrawn" }))).toBe("neutral");
    expect(noticeTone(notice({ status: "corrected" }))).toBe("neutral");
  });

  it("is caution while sent and unanswered", () => {
    expect(noticeTone(notice({ status: "marked_sent_by_customer" }))).toBe("caution");
    expect(noticeTone(notice({ status: "provider_responded", outcome: "no_response" }))).toBe("caution");
  });

  it("follows the provider's own answer once there is one", () => {
    expect(noticeTone(notice({ status: "provider_responded", outcome: "removed" }))).toBe("positive");
    expect(noticeTone(notice({ status: "provider_responded", outcome: "refused" }))).toBe("critical");
  });
});

describe("errorKey", () => {
  it("keys a known code, and falls back to the generic line for anything else", () => {
    expect(errorKey(new ApiError(409, "finding_not_eligible", {}))).toBe("notices.errors.codes.finding_not_eligible");
    expect(errorKey(new ApiError(409, "some_unknown_code", {}))).toBe("notices.errors.generic");
    expect(errorKey(new ApiError(404, "finding_not_eligible", {}))).toBe("notices.errors.generic");
    expect(errorKey(new Error("network"))).toBe("notices.errors.generic");
  });

  it("words the public host's own refusals too, as one sentence (ADR 0046)", () => {
    // A customer preparing a notice must hear that nothing was saved, not "something went wrong".
    // Which of our three plumbing reasons it was is not something they can act on.
    for (const code of ["processing_paused", "notice_write_unavailable", "too_many_requests"]) {
      expect(errorKey(new ApiError(503, code, {}))).toBe("notices.errors.codes.not_saved");
    }
  });
});

describe("isPendingWrite", () => {
  it("is the 202 the public host answers, and nothing else (ADR 0046)", () => {
    expect(isPendingWrite({ pending: true, submission_id: "sub_x" })).toBe(true);
    // A draft is not a pending write: showing the pending line for one would claim the opposite
    // of what happened, and caching it under an undefined id is the bug this guard prevents.
    expect(isPendingWrite(notice({}))).toBe(false);
    expect(isPendingWrite(null)).toBe(false);
    expect(isPendingWrite("pending")).toBe(false);
  });
});

describe("canExport", () => {
  it("needs an approved text: the confirmations come first", () => {
    expect(canExport(notice({ status: "draft", approved_text: null }))).toBe(false);
    expect(canExport(notice({ status: "customer_approved", approved_text: "text" }))).toBe(true);
    expect(canExport(notice({ status: "marked_sent_by_customer", approved_text: "text" }))).toBe(true);
  });

  it("is false once the draft is closed — the text stays on screen as the record, but it is not one to send", () => {
    expect(canExport(notice({ status: "withdrawn", approved_text: "text" }))).toBe(false);
    expect(canExport(notice({ status: "corrected", approved_text: "text" }))).toBe(false);
  });
});

describe("noticeExportPath", () => {
  it("points at the customer API, same origin, one path per format", () => {
    const id = "notice_" + "a".repeat(32);
    expect(EXPORT_FORMATS.map((format) => noticeExportPath(id, format))).toEqual([
      `/api/v1/app/notices/${id}/export/txt`,
      `/api/v1/app/notices/${id}/export/eml`,
      `/api/v1/app/notices/${id}/export/annex`,
    ]);
  });
});

describe("the submissions block's codes have words (issue #86, ADR 0046 §7)", () => {
  it.each([
    ["en", en],
    ["de", de],
  ])("in %s", (_language, resources) => {
    const notices = resources.notices;
    for (const code of SUBMISSION_CODES) expect(notices.errors.codes[code]).toBeTruthy();
    for (const kind of NOTICE_WRITE_KINDS) expect(notices.submissions.kinds[kind]).toBeTruthy();
    expect(notices.submissions.kinds.other).toBeTruthy();
    expect(notices.submissions.heading).toBeTruthy();
    expect(notices.submissions.processing).toBeTruthy();
    expect(notices.submissions.failed).toBeTruthy();
    expect(notices.submissions.refusedOther).toBeTruthy();
    expect(notices.submissions.refreshHint).toBeTruthy();
    for (const status of ["processing", "refused", "failed"] as const) {
      expect(notices.submissions.status[status]).toBeTruthy();
    }
  });
});

describe("isUnsettled", () => {
  it("is only false once a write has gone through — a write that went through needs no line", () => {
    expect(isUnsettled({ status: "processing" })).toBe(true);
    expect(isUnsettled({ status: "refused" })).toBe(true);
    expect(isUnsettled({ status: "failed" })).toBe(true);
    expect(isUnsettled({ status: "done" })).toBe(false);
  });
});

describe("submissionKindKey", () => {
  it("names every notice write kind the submissions endpoint reports, and falls back for anything else", () => {
    for (const kind of NOTICE_WRITE_KINDS) expect(submissionKindKey(kind)).toBe(`notices.submissions.kinds.${kind}`);
    expect(submissionKindKey("notice_exported")).toBe("notices.submissions.kinds.other");
    expect(submissionKindKey("something_unknown")).toBe("notices.submissions.kinds.other");
  });
});

describe("submissionMessage", () => {
  it("one line each for processing and failed, and the refusal's own words when it has any", () => {
    expect(submissionMessage({ status: "processing", code: "" })).toBe("notices.submissions.processing");
    expect(submissionMessage({ status: "failed", code: "" })).toBe("notices.submissions.failed");
    expect(submissionMessage({ status: "refused", code: "finding_rights_blocked" })).toBe(
      codeKey("finding_rights_blocked"),
    );
    expect(submissionMessage({ status: "refused", code: "finding_unavailable" })).toBe(
      codeKey("finding_unavailable"),
    );
    expect(submissionMessage({ status: "refused", code: "notice_not_found" })).toBe(
      "notices.errors.codes.notice_not_found",
    );
  });

  it("a code with no words of its own reads the neutral line — never the code itself", () => {
    expect(submissionMessage({ status: "refused", code: "no_organisation" })).toBe(
      "notices.submissions.refusedOther",
    );
  });
});

describe("blockerLine", () => {
  const word = (code: string) => (en.notices.blockers as Record<string, string>)[code] ?? "";

  it("separates whole sentences with a space, never a semicolon after a full stop", () => {
    const line = blockerLine(["identity_not_verified", "rights_record_missing"], word);
    expect(line).toBe(`${word("identity_not_verified")} ${word("rights_record_missing")}`);
    expect(line).not.toContain(".;");
    expect(line).not.toContain(";");
  });

  it("every blocker string is a whole sentence in both languages, which is what makes a space enough", () => {
    for (const resource of [en, de]) {
      const blockers = resource.notices.blockers as Record<string, string>;
      for (const [code, text] of Object.entries(blockers)) {
        expect(text.trimEnd().endsWith("."), `${code}: ${text}`).toBe(true);
      }
    }
  });

  it("one blocker is the sentence itself, and no blockers is the empty line", () => {
    expect(blockerLine(["identity_not_verified"], word)).toBe(word("identity_not_verified"));
    expect(blockerLine([], word)).toBe("");
  });
});
