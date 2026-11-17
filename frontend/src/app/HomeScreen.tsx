/**
 * The customer overview (`/app`): the ten-second answer to "is my work leaked, where, and what is
 * being done about it" (docs/customer-ui-plan.md §1), and — issue #32 — the state of the whole
 * catalogue, not just one original at a time: totals across every original and what changed most
 * recently, both derived from the same rows the per-original cards below already read.
 *
 * One card per original in one of three states — nothing found · found and online · found and
 * removed — with online copies first. The state is decided by **confirmed** copies only (the API's
 * rule); copies still being checked are counted beside it, never folded in. Below, the account the
 * signed-in user acts for, which is what scopes everything above it.
 */
import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { Field } from "../components/ui";
import { type MonitoredSources, type OriginalState, confirmedSites, sortOriginals, stateCounts } from "./caseFile";
import { catalogueTotals, recentChanges, rightsAttentionFor } from "./dashboard";
import { Card, LoadError, Loading, StateBadge, TITLE_ROW, TitleRow, useFormatDate, useFormatDay } from "./parts";
import type { Blocker } from "./rights";
import {
  type CustomerFinding,
  type CustomerOriginal,
  type Organisation,
  type Removal,
  useCustomerAccount,
  useCustomerFindings,
  useCustomerOriginals,
  useCustomerRemovals,
  useCustomerRights,
  useSubmissions,
} from "./queries";
import { type Submission, openSubmissions, submissionMessage } from "./submissions";

const SUMMARY_ORDER: readonly OriginalState[] = ["found_online", "found_removed", "nothing_found"];

export function CustomerHomeScreen() {
  const { t } = useTranslation();
  const account = useCustomerAccount();

  if (account.isPending) return <Loading />;
  if (account.isError) {
    return (
      <p role="alert" className="text-rose-800">
        {t("common.unreachable")}
      </p>
    );
  }

  const organisation = account.data.organisation;
  return (
    <div className="flex flex-col gap-5">
      {organisation ? (
        <Overview
          monitoredSources={account.data.monitored_sources}
          canAddOriginals={account.data.can_add_originals}
        />
      ) : (
        <p role="status" className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          {t("home.noOrganisation")}
        </p>
      )}
      {organisation ? <AccountCard organisation={organisation} /> : null}
    </div>
  );
}

function Overview({
  monitoredSources,
  canAddOriginals,
}: {
  monitoredSources: MonitoredSources;
  canAddOriginals: boolean;
}) {
  const { t } = useTranslation();
  const originals = useCustomerOriginals();
  const findings = useCustomerFindings();
  const removals = useCustomerRemovals();
  // Development-only today (ADR 0034): a failed or 404 read simply means no rights note anywhere
  // below, never an error of its own — the per-original cards read fine without it.
  const rights = useCustomerRights();
  const rightsAttention = rights.data ? rightsAttentionFor(originals.data ?? [], rights.data.works) : [];

  return (
    <section aria-labelledby="overview-heading" className="flex flex-col gap-3">
      <div>
        <h1 id="overview-heading" className="text-xl font-bold">
          {t("overview.heading")}
        </h1>
        <p className="mt-1 text-sm text-slate-600">{t("overview.lead")}</p>
        {canAddOriginals ? (
          <Link
            to="/app/originals/new"
            className="mt-2 inline-block rounded border border-sky-700 bg-sky-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-800"
          >
            {t("overview.addOriginal")}
          </Link>
        ) : null}
        {/*
          What is watched, never how busy we are: a static "monitoring active", the coverage bucket
          (never the count or the sites, 2026-09-16) and a fixed explanation. No queue, no activity
          and no "being checked" count — our workload is internal (operator decision, 2026-09-17).
        */}
        <div className="mt-2 flex flex-col gap-1 rounded border border-slate-200 bg-white p-3 text-sm text-slate-700">
          <p className="flex flex-wrap items-center gap-2">
            <span className="inline-block rounded bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-900">
              {t("overview.monitoring")}
            </span>
            <span>{t("coverage.watching", { sources: t(`coverage.sources.${monitoredSources}`) })}</span>
          </p>
          <p className="text-slate-600">{t("overview.explainer")}</p>
        </div>
      </div>
      {canAddOriginals ? <Submissions /> : null}
      {originals.isPending ? (
        <Loading />
      ) : originals.isError ? (
        <LoadError onRetry={() => void originals.refetch()} />
      ) : originals.data.length === 0 ? (
        <p className="rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-600">
          {canAddOriginals ? t("overview.emptyCanAdd") : t("overview.empty")}
        </p>
      ) : (
        <>
          <CatalogueTotals originals={originals.data} />
          <Summary originals={originals.data} />
          <RecentChanges originals={originals.data} findings={findings.data} removals={removals.data} />
          <ul className="flex flex-col gap-3">
            {sortOriginals(originals.data).map((original) => (
              <li key={original.id}>
                <OriginalCard
                  original={original}
                  findings={findings.data}
                  findingsFailed={findings.isError}
                  rightsBlockers={
                    rightsAttention.find((row) => row.originalId === original.id)?.blockers ?? null
                  }
                />
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

/** The catalogue as one line of numbers: how many originals, how many human-reviewed matching
 * copies across all of them, and how many of those are removed — the same counts each card below
 * shows for one original, summed. */
function CatalogueTotals({ originals }: { originals: readonly CustomerOriginal[] }) {
  const { t } = useTranslation();
  const totals = catalogueTotals(originals);
  return (
    <Card labelledBy="totals-heading">
      <h2 id="totals-heading" className="font-semibold">
        {t("overview.totals.heading")}
      </h2>
      <p className="mt-1 text-sm text-slate-700">
        {t("overview.totals.originals", { count: totals.originals })}
        {totals.confirmed > 0 ? (
          <> · {t("overview.confirmed", { count: totals.confirmed, removed: totals.removed })}</>
        ) : null}
      </p>
    </Card>
  );
}

const RECENT_CHANGES_LIMIT = 5;

/**
 * What changed most recently across the whole catalogue — a human-reviewed match, a removal, a
 * copy back online — merged from the same findings and removals rows the screens below already
 * read (`dashboard.recentChanges`). Renders nothing while either read has not settled yet or
 * failed: the totals and per-original cards above already carry the answer on their own.
 */
function RecentChanges({
  originals,
  findings,
  removals,
}: {
  originals: readonly CustomerOriginal[];
  findings: readonly CustomerFinding[] | undefined;
  removals: readonly Removal[] | undefined;
}) {
  const { t } = useTranslation();
  const formatDay = useFormatDay();
  const formatDate = useFormatDate();
  if (!findings || !removals) return null;
  const changes = recentChanges(originals, findings, removals, RECENT_CHANGES_LIMIT);

  return (
    <Card labelledBy="recent-changes-heading">
      <h2 id="recent-changes-heading" className="font-semibold">
        {t("overview.recent.heading")}
      </h2>
      {changes.length === 0 ? (
        <p className="mt-1 text-sm text-slate-600">{t("overview.recent.empty")}</p>
      ) : (
        <ul className="mt-2 flex flex-col gap-1 text-sm text-slate-700">
          {changes.map((change) => (
            <li key={`${change.kind}-${change.originalId}-${change.at}`}>
              <Link to="/app/originals/$originalId" params={{ originalId: change.originalId }} className="hover:underline">
                {t(`overview.recent.${change.kind}`, {
                  title: change.title,
                  date: change.kind === "confirmed" ? formatDay(change.at) : formatDate(change.at),
                })}
              </Link>
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
 * nothing when there is nothing open, and nothing on a failed read (the originals list still shows).
 */
function Submissions() {
  const { t } = useTranslation();
  const submissions = useSubmissions(true);
  const open = submissions.data ? openSubmissions(submissions.data) : [];
  if (open.length === 0) return null;
  return (
    <section
      aria-labelledby="submissions-heading"
      className="rounded-lg border border-slate-200 bg-white p-3 text-sm text-slate-700"
    >
      <h2 id="submissions-heading" className="font-semibold">
        {t("addOriginal.submissions.heading")}
      </h2>
      <ul className="mt-2 flex flex-col gap-2">
        {open.map((submission) => (
          <li key={submission.id} aria-label={submission.title}>
            <p className="flex flex-wrap items-center gap-2">
              <span className="font-medium break-words">{submission.title}</span>
              <span className={SUBMISSION_BADGE[submission.status]}>
                {t(`addOriginal.submissions.status.${submission.status}`)}
              </span>
            </p>
            <p role={submission.status === "processing" ? "status" : undefined} className="text-slate-600">
              {t(submissionMessage(submission))}
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}

const SUBMISSION_BADGE: Record<Submission["status"], string> = {
  processing: "inline-block rounded bg-sky-100 px-2 py-0.5 text-xs font-semibold text-sky-900",
  done: "inline-block rounded bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-900",
  refused: "inline-block rounded bg-slate-200 px-2 py-0.5 text-xs font-semibold text-slate-800",
  failed: "inline-block rounded bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900",
};

function Summary({ originals }: { originals: readonly CustomerOriginal[] }) {
  const { t } = useTranslation();
  const counts = stateCounts(originals);
  return (
    <ul aria-label={t("overview.heading")} className="flex flex-wrap gap-2 text-sm">
      {SUMMARY_ORDER.filter((state) => counts[state] > 0).map((state) => (
        <li key={state} className="flex items-center gap-2 rounded border border-slate-200 bg-white px-2 py-1">
          <StateBadge state={state} />
          <span>{t(`overview.summary.${state}`, { count: counts[state] })}</span>
        </li>
      ))}
    </ul>
  );
}

function OriginalCard({
  original,
  findings,
  findingsFailed,
  rightsBlockers,
}: {
  original: CustomerOriginal;
  findings: readonly CustomerFinding[] | undefined;
  findingsFailed: boolean;
  /** Rights steps still in the way of a notice draft for this original (ADR 0034); `null` when
   * there is nothing to say — no blocker, no rights row, or the rights read is not available here. */
  rightsBlockers: readonly Blocker[] | null;
}) {
  const { t } = useTranslation();
  const { counts } = original;
  const sites = findings ? confirmedSites(findings, original.id) : [];
  const params = { originalId: original.id };

  return (
    <article aria-label={original.title} className="rounded-lg border border-slate-200 bg-white p-4">
      <TitleRow
        title={
          <h2 title={original.title} className={`font-semibold ${TITLE_ROW.clamp}`}>
            <Link to="/app/originals/$originalId" params={params} className="hover:underline">
              {original.title}
            </Link>
          </h2>
        }
        badge={<StateBadge state={original.state} />}
      />
      <div className="mt-2 flex flex-col gap-1 text-sm text-slate-700">
        {counts.confirmed > 0 ? (
          <>
            <p>{t("overview.confirmed", { count: counts.confirmed, removed: counts.removed })}</p>
            {sites.length > 0 ? <p className="break-words">{t("overview.sites", { sites: sites.join(", ") })}</p> : null}
            {findingsFailed ? <p className="text-rose-800">{t("overview.sitesFailed")}</p> : null}
          </>
        ) : (
          <p>{t("overview.nothingFound")}</p>
        )}
      </div>
      {rightsBlockers && rightsBlockers.length > 0 ? (
        <div className="mt-2 rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">
          <p>{t("rights.works.missing")}</p>
          <ul className="ml-4 list-disc">
            {rightsBlockers.map((blocker) => (
              <li key={blocker}>{t(`rights.works.blockers.${blocker}`)}</li>
            ))}
          </ul>
          <Link to="/app/rights" className="mt-1 inline-block font-medium underline">
            {t("overview.rightsLink")}
          </Link>
        </div>
      ) : null}
      {counts.confirmed > 0 ? (
        <Link
          to="/app/originals/$originalId"
          params={params}
          className="mt-3 inline-block text-sm font-medium text-sky-800 underline"
        >
          {t("overview.openCase")}
        </Link>
      ) : null}
    </article>
  );
}

function AccountCard({ organisation }: { organisation: Organisation }) {
  const { t } = useTranslation();
  return (
    <Card labelledBy="account-heading">
      <h2 id="account-heading" className="font-semibold">
        {t("home.heading")}
      </h2>
      <p className="mt-1 mb-3 text-sm text-slate-600">{t("home.lead")}</p>
      <dl className="flex flex-col gap-1">
        <Field label={t("home.organisation")}>{organisation.name}</Field>
        <Field label={t("home.segment")}>{t(`home.segments.${organisation.segment}`)}</Field>
        <Field label={t("home.role")}>{t(`home.roles.${organisation.role}`)}</Field>
      </dl>
    </Card>
  );
}
