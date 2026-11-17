import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
// The merged resources (ADR 0044): two of the upload refusals keep their sentences in the
// overlay the Next app merges, so this is where every failure key's words really are.
import { customerDe as de, customerEn as en } from "../i18n/resources";
import {
  FAILURE_KEYS,
  RIGHTS_TEXT_VERSION,
  UPLOAD_DECLARATION_VERSION,
  castFrom,
  formProblems,
  formatFileSize,
  localFailure,
  normaliseName,
  problemKey,
  uploadFailure,
} from "./addOriginal";
import { MAX_UPLOAD_IMAGES } from "../lib/photoFingerprintFormat";

interface RightsConfirmation {
  current: string;
  versions: Record<string, Record<string, string>>;
}

/** The upload declaration (ADR 0044 amendment 2026-09-19): two statements per language. */
interface UploadDeclaration {
  current: string;
  versions: Record<string, Record<string, Record<string, string>>>;
}

// The server's copy of the declaration, read from the backend tree at transform time (like the PDQ
// fixtures in lib/pdq.test.ts): the words a customer ticks must be the words the record names.
const serverFiles = import.meta.glob<RightsConfirmation>(
  "../../../backend/marceline/app/rights_confirmation.json",
  { eager: true, import: "default" },
);
const server = Object.values(serverFiles)[0];

const declarationFiles = import.meta.glob<UploadDeclaration>(
  "../../../backend/marceline/app/upload_declaration.json",
  { eager: true, import: "default" },
);
const declaration = Object.values(declarationFiles)[0];

function lookup(resources: unknown, key: string): string | undefined {
  let node: unknown = resources;
  for (const part of key.split(".")) {
    if (!node || typeof node !== "object") return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : undefined;
}

describe("the rights declaration", () => {
  it("is the server's current version, word for word, in both languages", () => {
    expect(server).toBeDefined();
    expect(server?.current).toBe(RIGHTS_TEXT_VERSION);
    const texts = server?.versions[RIGHTS_TEXT_VERSION];
    expect(en.addOriginal.rights.statement).toBe(texts?.en);
    expect(de.addOriginal.rights.statement).toBe(texts?.de);
  });

  it("uses factual words only", () => {
    const everything = JSON.stringify([en.addOriginal, de.addOriginal, en.overview, de.overview]).toLowerCase();
    for (const word of ["leak", "infring", "illegal", "stolen", "gestohlen", "rechtswidrig"]) {
      expect(everything).not.toContain(word);
    }
  });
});

describe("the upload declaration (ADR 0044 amendment 2026-09-19)", () => {
  it("is the server's current version, word for word, in both languages", () => {
    expect(declaration).toBeDefined();
    expect(declaration?.current).toBe(UPLOAD_DECLARATION_VERSION);
    const texts = declaration?.versions[UPLOAD_DECLARATION_VERSION];
    expect(en.addOriginal.upload.declaration.solePerformer).toBe(texts?.en?.sole_performer);
    expect(en.addOriginal.upload.declaration.article9Consent).toBe(texts?.en?.article9_consent);
    expect(de.addOriginal.upload.declaration.solePerformer).toBe(texts?.de?.sole_performer);
    expect(de.addOriginal.upload.declaration.article9Consent).toBe(texts?.de?.article9_consent);
  });

  it("promises no duration and guarantees nothing, in either language", () => {
    for (const words of [en.addOriginal.upload.declaration, de.addOriginal.upload.declaration]) {
      const text = Object.values(words).join(" ");
      expect(text).not.toMatch(/innerhalb (von )?\d+ (Stunden?|Tagen?)|within \d+ (hours?|days?)/i);
      expect(text).not.toMatch(/garant|guarantee|wahrscheinlich|likely/i);
    }
  });

  it("names Art. 9 in the consent, because a consent that does not is not explicit", () => {
    expect(en.addOriginal.upload.declaration.article9Consent).toContain("Art. 9(2)(a) GDPR");
    expect(de.addOriginal.upload.declaration.article9Consent).toContain("Art. 9 Abs. 2");
  });
});

describe("the form's problems", () => {
  const filled = {
    file: new File([new Uint8Array([1])], "a.mp4", { type: "video/mp4" }),
    title: "A title",
    rightsHolder: "Sasha Meyer",
    others: "",
    confirmed: true,
  };

  it("does not ask for the upload declaration while the file stays on the computer", () => {
    expect(formProblems(filled)).toEqual([]);
  });

  it("asks for both of its ticks once the file is being sent", () => {
    expect(formProblems({ ...filled, sending: true })).toEqual(["notDeclared"]);
    expect(formProblems({ ...filled, sending: true, solePerformer: true })).toEqual(["notDeclared"]);
    expect(formProblems({ ...filled, sending: true, article9Consent: true })).toEqual([
      "notDeclared",
    ]);
    expect(
      formProblems({ ...filled, sending: true, solePerformer: true, article9Consent: true }),
    ).toEqual([]);
  });
});

describe("castFrom", () => {
  it("puts the rights holder first and keeps each name once, ignoring case and extra spaces", () => {
    expect(castFrom("  Sasha   Meyer ", "Carla Bremer\n\nsasha meyer\r\n  Carla  Bremer \nStudio Nord")).toEqual([
      "Sasha Meyer",
      "Carla Bremer",
      "Studio Nord",
    ]);
  });

  it("promotes nobody when the rights holder is missing", () => {
    expect(castFrom("   ", "Carla Bremer")).toEqual([]);
  });

  it("normalises whitespace like the server", () => {
    expect(normaliseName("\tA   B  ")).toBe("A B");
  });
});

describe("formProblems", () => {
  const file = new File([new Uint8Array([1])], "clip.mp4", { type: "video/mp4" });
  const complete = { file, title: "Beach day", rightsHolder: "Sasha Meyer", others: "", confirmed: true };

  it("accepts a complete form", () => {
    expect(formProblems(complete)).toEqual([]);
  });

  it("names everything that is missing, in the order of the form", () => {
    expect(formProblems({ file: null, title: " ", rightsHolder: "", others: "", confirmed: false })).toEqual([
      "missingFile",
      "missingTitle",
      "missingRightsHolder",
      "notConfirmed",
    ]);
  });

  it("refuses more names than the server takes, and names that are too long", () => {
    const many = Array.from({ length: 20 }, (_, index) => `Performer ${index}`).join("\n");
    expect(formProblems({ ...complete, others: many })).toEqual(["tooManyNames"]);
    expect(formProblems({ ...complete, rightsHolder: "x".repeat(256) })).toEqual(["nameTooLong"]);
    expect(formProblems({ ...complete, title: "t".repeat(256) })).toEqual(["titleTooLong"]);
  });

  it("has words for every problem in both languages", () => {
    for (const problem of Object.keys(en.addOriginal.problems)) {
      expect(lookup(de, `addOriginal.problems.${problem}`)).toBeTruthy();
    }
  });

  // Photo sets (issue #116, ADR 0053): the same form, counted in files instead of one file.
  it("takes a set of photographs in place of the one file", () => {
    const photos = [
      new File([new Uint8Array([1])], "beach-01.jpg", { type: "image/jpeg" }),
      new File([new Uint8Array([2])], "beach-02.jpg", { type: "image/jpeg" }),
    ];

    expect(formProblems({ ...complete, file: null, files: photos })).toEqual([]);
    expect(formProblems({ ...complete, file: null, files: [] })).toEqual(["missingFile"]);
  });

  it("refuses a set larger than one upload may carry", () => {
    const photo = new File([new Uint8Array([1])], "a.jpg", { type: "image/jpeg" });
    const many = Array.from({ length: MAX_UPLOAD_IMAGES + 1 }, () => photo);

    expect(formProblems({ ...complete, file: null, files: many })).toEqual(["tooManyImages"]);
  });

  it("says 'choose the photos' for a set and 'choose a video file' for a video", () => {
    expect(problemKey("missingFile", "video")).toBe("addOriginal.problems.missingFile");
    expect(problemKey("missingFile", "photos")).toBe("addOriginal.photos.problems.missingFiles");
    expect(problemKey("notConfirmed", "photos")).toBe("addOriginal.problems.notConfirmed");
    for (const resources of [en, de]) {
      expect(lookup(resources, problemKey("missingFile", "photos"))).toBeTruthy();
    }
  });
});

describe("failures", () => {
  function named(name: string, reason?: string): Error {
    const error = new Error("boom") as Error & { reason?: string };
    error.name = name;
    if (reason) error.reason = reason;
    return error;
  }

  it("words local failures by their reason, and everything else generically", () => {
    expect(localFailure(named("CodecUnsupportedError"))).toEqual({ key: "addOriginal.errors.codecUnsupported" });
    expect(localFailure(named("ClientFailedError", "too_long"))).toEqual({ key: "addOriginal.errors.local.too_long" });
    expect(localFailure(named("ClientFailedError", "cancelled"))).toEqual({ key: "addOriginal.errors.local.cancelled" });
    expect(localFailure(named("ClientFailedError", "worker_failed"))).toEqual({ key: "addOriginal.errors.local.other" });
    expect(localFailure("not even an error")).toEqual({ key: "addOriginal.errors.local.other" });
  });

  it("words a photo set's own failures as photographs, not as a video", () => {
    expect(localFailure(named("CodecUnsupportedError"), "photos")).toEqual({
      key: "addOriginal.photos.errors.codecUnsupported",
    });
    expect(localFailure(named("ClientFailedError", "no_usable_frames"), "photos")).toEqual({
      key: "addOriginal.photos.errors.noUsableImages",
    });
    // Its own reason and its own advice: send fewer photographs, not a shorter file.
    expect(localFailure(named("ClientFailedError", "too_many_images"), "photos")).toEqual({
      key: "addOriginal.errors.local.too_many_images",
    });
    // A cancel is the customer's own choice either way.
    expect(localFailure(named("ClientFailedError", "cancelled"), "photos")).toEqual({
      key: "addOriginal.errors.local.cancelled",
    });
  });

  it("words server refusals by their code, never by the server's text", () => {
    const refusal = (status: number, detail: string, retryAfter: number | null = null) =>
      new ApiError(status, detail, { detail, message: "" }, retryAfter);

    expect(uploadFailure(refusal(409, "claim_needs_review"))).toEqual({
      key: "addOriginal.errors.server.claim_needs_review",
    });
    expect(uploadFailure(refusal(400, "invalid_fingerprint"))).toEqual({ key: "addOriginal.errors.server.invalid" });
    expect(uploadFailure(refusal(503, "adding_originals_unavailable"))).toEqual({
      key: "addOriginal.errors.server.adding_originals_unavailable",
    });
    expect(uploadFailure(refusal(429, "too_many_requests", 125))).toEqual({
      key: "addOriginal.errors.server.too_many_requests_minutes",
      count: 3,
    });
    expect(uploadFailure(refusal(429, "too_many_requests"))).toEqual({
      key: "addOriginal.errors.server.too_many_requests",
    });
    expect(uploadFailure(new ApiError(401, "Unauthorized", {}))).toEqual({ key: "addOriginal.errors.server.signed_out" });
    expect(uploadFailure(new ApiError(500, "boom", "<html>"))).toEqual({ key: "addOriginal.errors.server.unexpected" });
    expect(uploadFailure(new TypeError("Failed to fetch"))).toEqual({ key: "addOriginal.errors.server.unreachable" });
  });

  it("has words for every failure in both languages", () => {
    for (const key of FAILURE_KEYS) {
      for (const resources of [en, de]) {
        const text = lookup(resources, key) ?? lookup(resources, `${key}_other`);
        expect(text, key).toBeTruthy();
      }
    }
  });
});

describe("formatFileSize", () => {
  it("writes sizes the way the language does", () => {
    expect(formatFileSize(1_400_000_000, "en")).toBe("1.4 GB");
    expect(formatFileSize(1_400_000_000, "de")).toBe("1,4 GB");
    expect(formatFileSize(512, "en")).toBe("512 B");
    expect(formatFileSize(250_000_000, "en")).toBe("250 MB");
  });
});
