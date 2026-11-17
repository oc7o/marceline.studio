/**
 * German strings for the customer UI. The `Translation` annotation is deliberate: it makes a key
 * that exists in `en.ts` but not here a typecheck error rather than an English word appearing in a
 * German sentence at runtime.
 *
 * Form of address: "du", lower-case, like the public website (operator decision 2026-09-19).
 * Wording rules (frontend/CLAUDE.md): technical facts only, no legal verdicts.
 *
 * The words live in `de/`, one slice per screen, exactly as the English ones do — see `en.ts` for
 * why and for the three sections that are split across two modules.
 */
import type { Translation } from "./en";
import { deAccountEmail } from "./de/accountEmail";
import { deAddOriginal } from "./de/addOriginal";
import { deCaseFile } from "./de/caseFile";
import { deCore } from "./de/core";
import { deDataRequests } from "./de/dataRequests";
import { deNotices } from "./de/notices";
import { deOnboarding } from "./de/onboarding";
import { deRights } from "./de/rights";
import { deVerification } from "./de/verification";

export const de: Translation = {
  ...deCore,
  ...deAccountEmail,
  ...deNotices,
  ...deOnboarding,
  ...deVerification,
  ...deDataRequests,
  addOriginal: { ...deCore.addOriginal, ...deAddOriginal.addOriginal },
  caseFile: {
    ...deCaseFile.caseFile,
    where: { ...deCore.caseFile.where, ...deCaseFile.caseFile.where },
  },
  rights: {
    ...deRights.rights,
    works: { ...deCore.rights.works, ...deRights.rights.works },
  },
};
