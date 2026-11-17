/**
 * The customer overview (`/app`): the ten-second answer to "has my work been copied, where, and
 * what has happened since" (docs/customer-ui-plan.md §1), and — issue #32 — the state of the whole
 * catalogue rather than one original at a time: totals across every original and what changed most
 * recently, both derived from the same rows the per-original cards below already read.
 *
 * One card per original in one of three states — nothing found · found and online · found and
 * removed — with online copies first. The state is decided by human-reviewed copies only (the
 * API's rule). Below, the account the signed-in user acts for, which is what scopes everything
 * above it.
 *
 * A **synchronous server component**: every fact it renders arrives as a prop, so the page above
 * it does the reading and a test renders it to a string without a request.
 */
import {
  type MonitoredSources,
  type OriginalState,
  confirmedSites,
  sortOriginals,
  stateCounts,
} from "@shared/app/caseFile";
import {
  type RightsAttention,
  catalogueTotals,
  recentChanges,
  rightsAttentionFor,
} from "@shared/app/dashboard";
import { pageHref, pageLink, paginate } from "@shared/app/pagination";
import type { Blocker } from "@shared/app/rights";
import { type Submission, openSubmissions, submissionMessage } from "@shared/app/submissions";

import { OpenUploads } from "../components/OpenUploads";
import { Refresher } from "../components/Refresher";
import {
  BADGE,
  Card,
  LoadError,
  StateBadge,
  TITLE_ROW,
  TitleRow,
  formatDate,
  formatDay,
} from "../components/parts";
import {
  Banner,
  DefinitionList,
  EmptyState,
  Field,
  LinkButton,
  Pagination,
  Veil,
} from "../components/ui";
import { SUBMISSION_TONES } from "../lib/tones";
import type {
  CustomerAccount,
  CustomerFinding,
  CustomerOriginal,
  CustomerUpload,
  Removal,
  RightsOverview,
} from "../lib/reads";
import type { Words } from "../lib/words";

const SUMMARY_ORDER: readonly OriginalState[] = ["found_online", "found_removed", "nothing_found"];

/** How often the overview asks again while an upload is being added (production). */
export const SUBMISSION_POLL_MS = 15_000;

/** How many recent changes the overview lists. */
const RECENT_CHANGES_LIMIT = 5;

export interface HomeData {
  account: CustomerAccount;
  /** null when the read failed; the screen says so rather than showing an empty list. */
  originals: CustomerOriginal[] | null;
  findings: CustomerFinding[] | null;
  submissions: Submission[] | null;
  removals: Removal[] | null;
  /** Development-only today (ADR 0034): null simply means no rights note anywhere below. */
  rights: RightsOverview | null;
  /**
   * The files being fingerprinted for this organisation (issue #59, ADR 0044), or null where
   * cloud fingerprinting is off — which is everywhere until the operator switches it on.
   */
  uploads?: CustomerUpload[] | null;
}

export function HomeScreen({
  i18n,
  data,
  /** The `?page=` originals are shown at (issue #134); 1 when the page has no such query. */
  page = 1,
}: {
  i18n: Words;
  data: HomeData;
  page?: number;
}) {
  const organisation = data.account.organisation;
  return (
    <div className="flex flex-col gap-5">
      {organisation ? (
        <Overview i18n={i18n} data={data} page={page} />
      ) : (
        <Banner tone="caution" role="status">
          {i18n.t("home.noOrganisation")}
        </Banner>
      )}
      {organisation ? (
        <Card labelledBy="account-heading">
          <h2 id="account-heading" className="font-display text-display-s text-ink">
            {i18n.t("home.heading")}
          </h2>
          <p className="mt-1 mb-3 text-body text-ink-muted">{i18n.t("home.lead")}</p>
          <DefinitionList>
            <Field label={i18n.t("home.organisation")}>{organisation.name}</Field>
            <Field label={i18n.t("home.segment")}>
              {i18n.t(`home.segments.${organisation.segment}`)}
            </Field>
            <Field label={i18n.t("home.role")}>{i18n.t(`home.roles.${organisation.role}`)}</Field>
          </DefinitionList>
        </Card>
      ) : null}
    </div>
  );
}

function Overview({ i18n, data, page }: { i18n: Words; data: HomeData; page: number }) {
  const canAdd = data.account.can_add_originals;
  const monitoredSources: MonitoredSources = data.account.monitored_sources;
  const { originals, findings, removals, rights } = data;
  const rightsAttention = rights ? rightsAttentionFor(originals ?? [], rights.works) : [];
  // One primary action per view (the system's "spend the red"): with nothing in the account the
  // empty state below carries it, so the header does not offer it twice.
  const headerAction = canAdd && originals?.length !== 0;

  return (
    <section aria-labelledby="overview-heading" className="flex flex-col gap-4">
      <div>
        {/*
          The one Veil of this view (the system allows one, behind chrome only): the page header,
          never the cards below it. An empty state here therefore asks for none.
        */}
        <header className="relative isolate overflow-hidden rounded-md px-4 py-5">
          <Veil />
          <h1 id="overview-heading" className="font-display text-display-l text-ink">
            {i18n.t("overview.heading")}
          </h1>
          <p className="mt-2 max-w-[68ch] text-body-l text-ink-muted">{i18n.t("overview.lead")}</p>
          {headerAction ? (
            <p className="mt-4">
              <LinkButton href="/app/originals/new">{i18n.t("overview.addOriginal")}</LinkButton>
            </p>
          ) : null}
        </header>
        {/*
          What is watched, never how busy we are: a static "monitoring active", the coverage bucket
          (never the count or the sites, operator 2026-09-16) and a fixed explanation. No queue, no
          activity and no "being checked" count — our workload is internal (operator 2026-09-17).
        */}
        <div className="mt-3 flex flex-col gap-1 rounded-md border border-hairline bg-surface-100 p-4 text-body-s text-ink">
          <p className="flex flex-wrap items-center gap-2">
            <span className={`${BADGE} bg-positive-quiet text-positive-ink`}>
              {i18n.t("overview.monitoring")}
            </span>
            <span>
              {i18n.t("coverage.watching", {
                sources: i18n.t(`coverage.sources.${monitoredSources}`),
              })}
            </span>
          </p>
          <p className="text-ink-muted">{i18n.t("overview.explainer")}</p>
        </div>
      </div>
      {canAdd ? <Submissions i18n={i18n} submissions={data.submissions} /> : null}
      {/* The customer may close the add page while we fingerprint; this is where they find it. */}
      <OpenUploads i18n={i18n} uploads={data.uploads ?? null} />
      {originals === null ? (
        <LoadError i18n={i18n} href="/app" />
      ) : originals.length === 0 ? (
        <EmptyState
          body={canAdd ? i18n.t("overview.emptyCanAdd") : i18n.t("overview.empty")}
          next={i18n.t("overview.emptyNext")}
          action={
            canAdd ? (
              <LinkButton href="/app/originals/new" block>
                {i18n.t("overview.addOriginal")}
              </LinkButton>
            ) : undefined
          }
        />
      ) : (
        <>
          <CatalogueTotals i18n={i18n} originals={originals} />
          <Summary i18n={i18n} originals={originals} />
          <RecentChanges
            i18n={i18n}
            originals={originals}
            findings={findings}
            removals={removals}
          />
          <OriginalsList
            i18n={i18n}
            originals={originals}
            page={page}
            findings={findings}
            rightsAttention={rightsAttention}
          />
        </>
      )}
    </section>
  );
}

/**
 * One card per original, paginated (issue #134): the API sends every original the organisation
 * owns in one answer (no cursor), so this still reads the whole list — it only caps what gets
 * rendered, the same way the read-more-per-page pattern already built for `Pagination` intends.
 * `originals` is the **unsorted** full list; the sort decides page order, so the same original is
 * always on the same page across requests.
 */
function OriginalsList({
  i18n,
  originals,
  page,
  findings,
  rightsAttention,
}: {
  i18n: Words;
  originals: readonly CustomerOriginal[];
  page: number;
  findings: readonly CustomerFinding[] | null;
  rightsAttention: readonly RightsAttention[];
}) {
  const sorted = sortOriginals(originals);
  const paged = paginate(sorted, page);
  const previousPage = paged.page > 1 ? paged.page - 1 : null;
  const nextPage = paged.page < paged.pageCount ? paged.page + 1 : null;
  return (
    <>
      <ul className="flex flex-col gap-3">
        {paged.items.map((original) => (
          <li key={original.id}>
            <OriginalCard
              i18n={i18n}
              original={original}
              findings={findings ?? undefined}
              findingsFailed={findings === null}
              rightsBlockers={
                rightsAttention.find((row) => row.originalId === original.id)?.blockers ?? null
              }
            />
          </li>
        ))}
      </ul>
      {paged.pageCount > 1 ? (
        <Pagination
          label={i18n.t("pagination.label")}
          status={i18n.t("pagination.status", {
            start: paged.start,
            end: paged.end,
            total: paged.total,
          })}
          previous={pageLink(i18n.t("pagination.previous"), pageHref("/app", previousPage))}
          next={pageLink(i18n.t("pagination.next"), pageHref("/app", nextPage))}
        />
      ) : null}
    </>
  );
}

/**
 * The catalogue as one line of numbers: how many originals, how many human-reviewed matching copies
 * across all of them, and how many of those are removed — the same counts each card below shows for
 * one original, summed.
 */
function CatalogueTotals({
  i18n,
  originals,
}: {
  i18n: Words;
  originals: readonly CustomerOriginal[];
}) {
  const totals = catalogueTotals(originals);
  return (
    <Card labelledBy="totals-heading">
      <h2 id="totals-heading" className="font-display text-display-s text-ink">
        {i18n.t("overview.totals.heading")}
      </h2>
      <p className="mt-1 text-body text-ink">
        {i18n.t("overview.totals.originals", { count: totals.originals })}
        {totals.confirmed > 0 ? (
          <>
            {" · "}
            {i18n.t("overview.confirmed", {
              count: totals.confirmed,
              removed: totals.removed,
            })}
          </>
        ) : null}
      </p>
    </Card>
  );
}

/**
 * What changed most recently across the whole catalogue — a human-reviewed match, a removal, a copy
 * back online — merged from the same findings and removals rows the screens below already read
 * (`dashboard.recentChanges`). Renders nothing when either read failed: the totals and the cards
 * above already carry the answer on their own.
 */
function RecentChanges({
  i18n,
  originals,
  findings,
  removals,
}: {
  i18n: Words;
  originals: readonly CustomerOriginal[];
  findings: readonly CustomerFinding[] | null;
  removals: readonly Removal[] | null;
}) {
  if (!findings || !removals) return null;
  const changes = recentChanges(originals, findings, removals, RECENT_CHANGES_LIMIT);
  return (
    <Card labelledBy="recent-changes-heading">
      <h2 id="recent-changes-heading" className="font-display text-display-s text-ink">
        {i18n.t("overview.recent.heading")}
      </h2>
      {changes.length === 0 ? (
        <p className="mt-1 text-body text-ink-muted">{i18n.t("overview.recent.empty")}</p>
      ) : (
        <ul className="mt-2 flex flex-col gap-1 text-body text-ink">
          {changes.map((change) => (
            <li key={`${change.kind}-${change.originalId}-${change.at}`}>
              <a
                href={`/app/originals/${change.originalId}`}
                className="rounded-sm text-brand-ink hover:underline"
              >
                {i18n.t(`overview.recent.${change.kind}`, {
                  title: change.title,
                  date:
                    change.kind === "confirmed"
                      ? formatDay(change.at, i18n.language)
                      : formatDate(change.at, i18n.language),
                })}
              </a>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

/**
 * The customer's own uploads that are being added or were not added (production, ADR 0028 write
 * path). Nothing about our review work: only what happened to what this organisation sent. Renders
 * nothing when there is nothing open, and nothing on a failed read (the originals still show).
 */
function Submissions({ i18n, submissions }: { i18n: Words; submissions: Submission[] | null }) {
  const open = submissions ? openSubmissions(submissions) : [];
  if (open.length === 0) return null;
  const processing = open.some((submission) => submission.status === "processing");
  return (
    <section
      aria-labelledby="submissions-heading"
      className="rounded-md border border-hairline bg-surface-100 p-4 text-body-s text-ink"
    >
      <h2 id="submissions-heading" className="font-display text-display-s text-ink">
        {i18n.t("addOriginal.submissions.heading")}
      </h2>
      {/* Only while something is actually being added; a settled list is not re-read. */}
      {processing ? <Refresher intervalMs={SUBMISSION_POLL_MS} /> : null}
      <ul className="mt-2 flex flex-col gap-2">
        {open.map((submission) => (
          <li key={submission.id} aria-label={submission.title}>
            <p className="flex flex-wrap items-center gap-2">
              <span className="font-medium break-words text-ink">{submission.title}</span>
              <span className={`${BADGE} ${SUBMISSION_TONES[submission.status]}`}>
                {i18n.t(`addOriginal.submissions.status.${submission.status}`)}
              </span>
            </p>
            <p
              role={submission.status === "processing" ? "status" : undefined}
              className="text-ink-muted"
            >
              {i18n.t(submissionMessage(submission))}
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}


function Summary({ i18n, originals }: { i18n: Words; originals: readonly CustomerOriginal[] }) {
  const counts = stateCounts(originals);
  return (
    <ul aria-label={i18n.t("overview.heading")} className="flex flex-wrap gap-2 text-body-s">
      {SUMMARY_ORDER.filter((state) => counts[state] > 0).map((state) => (
        <li
          key={state}
          className="flex items-center gap-2 rounded-sm border border-hairline bg-surface-100 px-2 py-1"
        >
          <StateBadge state={state} i18n={i18n} />
          <span>{i18n.t(`overview.summary.${state}`, { count: counts[state] })}</span>
        </li>
      ))}
    </ul>
  );
}

function OriginalCard({
  i18n,
  original,
  findings,
  findingsFailed,
  rightsBlockers,
}: {
  i18n: Words;
  original: CustomerOriginal;
  findings: readonly CustomerFinding[] | undefined;
  findingsFailed: boolean;
  /** Rights steps still in the way of a notice draft for this original (ADR 0034); `null` when
   *  there is nothing to say — no blocker, no rights row, or no rights read here. */
  rightsBlockers: readonly Blocker[] | null;
}) {
  const { counts } = original;
  const sites = findings ? confirmedSites(findings, original.id) : [];
  const href = `/app/originals/${original.id}`;

  return (
    <article
      aria-label={original.title}
      className="rounded-md border border-hairline bg-surface-100 p-4 shadow-sm"
    >
      <TitleRow
        title={
          <h2 title={original.title} className={`text-body-l font-medium ${TITLE_ROW.clamp}`}>
            <a href={href} className="rounded-sm hover:underline">
              {original.title}
            </a>
          </h2>
        }
        badge={<StateBadge state={original.state} i18n={i18n} />}
      />
      <div className="mt-2 flex flex-col gap-1 text-body-s text-ink">
        {counts.confirmed > 0 ? (
          <>
            <p>
              {i18n.t("overview.confirmed", {
                count: counts.confirmed,
                removed: counts.removed,
              })}
            </p>
            {sites.length > 0 ? (
              <p className="break-words">{i18n.t("overview.sites", { sites: sites.join(", ") })}</p>
            ) : null}
            {findingsFailed ? (
              <p className="text-critical-ink">{i18n.t("overview.sitesFailed")}</p>
            ) : null}
          </>
        ) : (
          <p>{i18n.t("overview.nothingFound")}</p>
        )}
      </div>
      {rightsBlockers && rightsBlockers.length > 0 ? (
        <div className="mt-3 rounded-md border border-caution-ink/40 bg-caution-quiet p-3 text-caption text-caution-ink">
          <p>{i18n.t("rights.works.missing")}</p>
          <ul className="ml-4 list-disc">
            {rightsBlockers.map((blocker) => (
              <li key={blocker}>{i18n.t(`rights.works.blockers.${blocker}`)}</li>
            ))}
          </ul>
          <a href="/app/rights" className="tap mt-1 font-medium underline">
            {i18n.t("overview.rightsLink")}
          </a>
        </div>
      ) : null}
      {counts.confirmed > 0 ? (
        <a href={href} className="tap mt-3 rounded-sm text-button text-brand-ink underline">
          {i18n.t("overview.openCase")}
        </a>
      ) : null}
    </article>
  );
}
