/**
 * English strings for the customer UI. **This file is the shape of the resource set**: `de.ts` is
 * annotated with `Translation`, so a key added here and forgotten there is a typecheck error, and
 * `i18n.test.ts` fails on any key (or interpolation placeholder) present in one language only.
 *
 * Never put a user-visible string in a component. A half-translated UI is the failure mode this
 * seam exists to prevent (docs/customer-ui-plan.md: bilingual from the start).
 *
 * **The words themselves live in `en/`, one slice per screen** (issue #99). The SPA used to load
 * this whole file in its `/app` first load — 11.46 KB gzip of 149.88, for a screen that names about
 * a sixth of it — so `rights`, `notices`, `caseFile` and `addOriginal`, which are four fifths of
 * the words and belong to one route each, are loaded by that route (`i18n/index.ts`,
 * `loadScreenWords`). This module puts them back together for everything that wants the whole
 * resource: the tests that guard key parity, placeholder parity and the wording rules, and the
 * Next app, which renders on the server and ships no resource to a browser at all.
 *
 * Three sections are split across two modules, because the shell and the overview name a few of
 * their keys (`caseFile.where.open`, `addOriginal.submissions`, `rights.works.missing` and
 * `.blockers`): those keys live in `en/core.ts` and are merged back here, so no key path changed
 * and no string exists twice. Every other section belongs to exactly one slice — `i18n.test.ts`
 * checks that.
 */
import { enAccountEmail } from "./en/accountEmail";
import { enAddOriginal } from "./en/addOriginal";
import { enCaseFile } from "./en/caseFile";
import { enCore } from "./en/core";
import { enDataRequests } from "./en/dataRequests";
import { enNotices } from "./en/notices";
import { enOnboarding } from "./en/onboarding";
import { enRights } from "./en/rights";
import { enVerification } from "./en/verification";

export const en = {
  ...enCore,
  ...enAccountEmail,
  ...enNotices,
  ...enOnboarding,
  ...enVerification,
  ...enDataRequests,
  addOriginal: { ...enCore.addOriginal, ...enAddOriginal.addOriginal },
  caseFile: {
    ...enCaseFile.caseFile,
    where: { ...enCore.caseFile.where, ...enCaseFile.caseFile.where },
  },
  rights: {
    ...enRights.rights,
    works: { ...enCore.rights.works, ...enRights.rights.works },
  },
};

/** The resource shape every language must match exactly. */
export type Translation = typeof en;
