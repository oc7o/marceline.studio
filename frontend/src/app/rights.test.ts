/**
 * Rights helpers (ADR 0034): every code the API can send has words in both languages — template
 * literal `t()` keys are invisible to `translationKeys.test.ts` — and refusals map to their words.
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { de } from "../i18n/de";
import { en } from "../i18n/en";
import {
  ASSERTED_RIGHTS,
  BLOCKERS,
  CAPACITIES,
  CLAIMANT_TYPES,
  ERROR_CODES,
  IDENTITY_METHODS,
  IDENTITY_STATUSES,
  PARTNER_KINDS,
  PROOF_KINDS,
  RIGHTS_STATUSES,
  WITHDRAWAL_REASONS,
  canDeclare,
  canDeclareAuthority,
  canWithdraw,
  canWithdrawAuthority,
  declarationLanguage,
  errorKey,
  formatBytes,
  proofForm,
} from "./rights";

describe("rights codes have words", () => {
  it.each([
    ["en", en],
    ["de", de],
  ])("in %s", (_language, resources) => {
    const rights = resources.rights;
    for (const code of CLAIMANT_TYPES) expect(rights.claimant.types[code]).toBeTruthy();
    for (const code of IDENTITY_STATUSES) expect(rights.claimant.identityStatus[code]).toBeTruthy();
    for (const code of IDENTITY_METHODS) expect(rights.claimant.identityMethod[code]).toBeTruthy();
    for (const code of RIGHTS_STATUSES) expect(rights.works.status[code]).toBeTruthy();
    for (const code of CAPACITIES) expect(rights.works.capacities[code]).toBeTruthy();
    for (const code of ASSERTED_RIGHTS) expect(rights.works.rights[code]).toBeTruthy();
    for (const code of BLOCKERS) expect(rights.works.blockers[code]).toBeTruthy();
    for (const code of PARTNER_KINDS) expect(rights.partners.kinds[code]).toBeTruthy();
    for (const code of PROOF_KINDS) expect(rights.proofs.kinds[code]).toBeTruthy();
    for (const code of ERROR_CODES) expect(rights.errors.codes[code]).toBeTruthy();
    for (const code of WITHDRAWAL_REASONS) expect(rights.works.withdrawReasons[code]).toBeTruthy();
  });

  it("never calls anything an infringement", () => {
    const text = JSON.stringify([en.rights, de.rights]).toLowerCase();
    expect(text).not.toContain("infringement");
    expect(text).not.toContain("rechtsverletzung");
  });
});

describe("errorKey", () => {
  it("names the refusal's own words, or a generic line", () => {
    expect(errorKey(new ApiError(409, "partners_exist", { detail: "partners_exist" }))).toBe(
      "rights.errors.codes.partners_exist",
    );
    expect(errorKey(new ApiError(415, "unsupported_type", {}))).toBe("rights.errors.codes.unsupported_type");
    expect(errorKey(new ApiError(409, "something_new", {}))).toBe("rights.errors.generic");
    expect(errorKey(new ApiError(404, "Original not found", {}))).toBe("rights.errors.generic");
    expect(errorKey(new Error("network"))).toBe("rights.errors.generic");
  });
});

describe("helpers", () => {
  it("picks the declaration language from the UI language", () => {
    expect(declarationLanguage("de")).toBe("de");
    expect(declarationLanguage("de-AT")).toBe("de");
    expect(declarationLanguage("en")).toBe("en");
  });

  it("formats the upload limit", () => {
    expect(formatBytes(10 * 1024 * 1024, "en")).toBe("10 MB");
    expect(formatBytes(512 * 1024, "de")).toBe("512 KB");
  });

  it("only unverified and self-declared works can be declared", () => {
    const work = {
      original_id: "orig_1",
      title: "x",
      capacity: null,
      underlying_owner_name: null,
      asserted_right: null,
      asserted_right_detail: "",
      declared_at: null,
      declaration_version: null,
      authority_declared_at: null,
      authority_declaration_version: null,
      notice_blockers: [],
    };
    expect(canDeclare({ ...work, rights_status: "unverified" })).toBe(true);
    expect(canDeclare({ ...work, rights_status: "self_declared" })).toBe(true);
    expect(canDeclare({ ...work, rights_status: "documents_received" })).toBe(false);
    expect(canDeclare({ ...work, rights_status: "disputed" })).toBe(false);
    expect(canWithdraw({ ...work, rights_status: "self_declared" })).toBe(true);
    expect(canWithdraw({ ...work, rights_status: "unverified" })).toBe(false);
    expect(canWithdraw({ ...work, rights_status: "documents_received" })).toBe(false);
    expect(canWithdraw({ ...work, rights_status: "documents_complete" })).toBe(false);
  });

  it("the authority declaration belongs to documents_complete alone, and is withdrawn from authority_declared", () => {
    const work = {
      original_id: "orig_1",
      title: "x",
      capacity: null,
      underlying_owner_name: null,
      asserted_right: null,
      asserted_right_detail: "",
      declared_at: null,
      declaration_version: null,
      authority_declared_at: null,
      authority_declaration_version: null,
      notice_blockers: [],
    };
    expect(canDeclareAuthority({ ...work, rights_status: "documents_complete" })).toBe(true);
    for (const status of ["unverified", "self_declared", "documents_received", "authority_declared", "disputed"] as const) {
      expect(canDeclareAuthority({ ...work, rights_status: status })).toBe(false);
    }
    expect(canWithdrawAuthority({ ...work, rights_status: "authority_declared" })).toBe(true);
    expect(canWithdrawAuthority({ ...work, rights_status: "documents_complete" })).toBe(false);
  });

  it("builds the multipart body", () => {
    const form = proofForm(new File(["%PDF-"], "a.pdf"), "licence", "orig_7", " p. 2 ");
    expect(form.get("kind")).toBe("licence");
    expect(form.get("original_id")).toBe("orig_7");
    expect(form.get("note")).toBe("p. 2");
    expect(proofForm(new File(["x"], "a.pdf"), "other", null, "").has("original_id")).toBe(false);
  });
});
