import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { type Schemas, api, errorMessage, unwrap } from "../api/client";
import { Button } from "../components/ui";
import { dateTime } from "../lib/format";
import {
  type Tone,
  ago,
  barWidth,
  budgetTone,
  bytesLabel,
  count,
  durationLabel,
  loadTone,
  rate,
  share,
} from "../lib/overviewFormat";

type SiteStats = Schemas["SiteStatsOut"];
type Job = Schemas["JobOut"];
type M2 = Schemas["M2Out"];
type ReviewWork = Schemas["ReviewWorkOut"];
type Resources = Schemas["ResourcesOut"];

const REFRESH_MS = 60_000;
export const overviewQueryKey = ["overview"] as const;

const BAR_TONES: Record<Tone | "info", string> = {
  ok: "bg-positive-ink",
  warn: "bg-caution-ink",
  bad: "bg-critical",
  info: "bg-accent",
};

/** Start page: counts and small bars only. Refreshes every 60 s; no media, no external resources. */
export function OverviewScreen() {
  const query = useQuery({
    queryKey: overviewQueryKey,
    queryFn: () => unwrap(api.GET("/api/v1/review/overview")),
    refetchInterval: REFRESH_MS,
  });
  if (query.isPending) return <p className="text-ink-subtle">Loading…</p>;
  if (query.isError) return <p className="text-critical-ink">Could not load: {errorMessage(query.error)}</p>;
  const data = query.data;
  const now = new Date(data.generated_at);
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-display text-display-m">Overview</h1>
        <span className="text-body-s text-ink-muted">
          as of {dateTime(data.generated_at)} UTC · refreshes every minute
        </span>
        <Button className="ml-auto" onClick={() => void query.refetch()} disabled={query.isFetching}>
          {query.isFetching ? "Refreshing…" : "Refresh"}
        </Button>
      </div>
      <ResourcesSection resources={data.resources} now={now} />
      <ReviewCards review={data.review} />
      <M2Card m2={data.m2} />
      <SitesTable sites={data.sites} />
      <JobsTable jobs={data.jobs} durations={data.resources.scheduler} now={now} />
    </div>
  );
}

function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="rounded-md border border-hairline bg-surface-100 p-4">
      <div className="mb-3 flex flex-wrap items-baseline gap-3">
        <h2 className="font-semibold">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Bar({ part, whole, tone = "info", label }: { part: number; whole: number; tone?: Tone | "info"; label: string }) {
  return (
    <div
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(share(part, whole) * 100)}
      className="h-1.5 w-full min-w-16 overflow-hidden rounded-full bg-surface-300"
    >
      <div className={`h-full ${BAR_TONES[tone]}`} style={{ width: barWidth(part, whole) }} />
    </div>
  );
}

function ReviewCards({ review }: { review: ReviewWork }) {
  const safetyOpen = review.safety > 0;
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Link to="/references" search={{ status: "proposed" }} className="rounded-md border border-hairline bg-surface-100 p-4 hover:border-hairline-strong">
        <div className="text-body-s text-ink-muted">Reference pairs to review</div>
        <div className="text-2xl font-semibold">{count(review.reference_pairs)}</div>
        <div className="text-caption text-ink-subtle">
          {count(review.reference_leaks)} leaks · {count(review.reference_matches)} matches proposed
        </div>
      </Link>
      <Link to="/clusters" className="rounded-md border border-hairline bg-surface-100 p-4 hover:border-hairline-strong">
        <div className="text-body-s text-ink-muted">Clusters with proposed members</div>
        <div className="text-2xl font-semibold">{count(review.clusters)}</div>
      </Link>
      <Link
        to="/safety"
        className={`rounded-md border p-4 ${
          safetyOpen ? "border-critical-ink bg-critical-quiet text-critical-ink hover:border-critical" : "border-hairline bg-surface-100 hover:border-hairline-strong"
        }`}
      >
        <div className="text-body-s">Safety review</div>
        <div className="text-2xl font-semibold">{count(review.safety)}</div>
        {safetyOpen ? <div className="text-caption font-semibold">Pages waiting: decide these first</div> : null}
        <div className="text-caption text-ink-subtle">{count(review.safety_blocked)} blocked in total</div>
      </Link>
      <Link to="/candidates" className="rounded-md border border-hairline bg-surface-100 p-4 hover:border-hairline-strong">
        <div className="text-body-s text-ink-muted">Source candidates proposed</div>
        <div className="text-2xl font-semibold">{count(review.source_candidates)}</div>
      </Link>
    </div>
  );
}

function M2Card({ m2 }: { m2: M2 }) {
  const recalls = [
    { label: "strong", value: m2.recall_strong },
    { label: "strong + weak", value: m2.recall_strong_weak },
    { label: "strong + weak + linked (exit check)", value: m2.recall_strong_weak_linked },
  ];
  const falseRate = m2.false_match_rate;
  return (
    <Section
      title="M2 detection"
      aside={
        <>
          <span
            className={`rounded-sm px-2 py-0.5 text-caption font-semibold ${
              m2.passed ? "bg-positive-quiet text-positive-ink" : "bg-critical-quiet text-critical-ink"
            }`}
          >
            exit {m2.passed ? "PASS" : "FAIL"}
          </span>
          {m2.too_few_to_judge ? <span className="text-caption font-semibold text-caution-ink">too few to judge</span> : null}
          <span className="text-caption text-ink-subtle">
            sample: {count(m2.confirmed)} confirmed leaks, {count(m2.reviewed_matches)} reviewed matches · distance ≤{" "}
            {m2.max_distance}
          </span>
        </>
      }
    >
      <div className="grid gap-4 md:grid-cols-2">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-body-s">
          <dt className="text-ink-subtle">Confirmed leaks</dt>
          <dd>{count(m2.confirmed)}</dd>
          <dt className="text-ink-subtle">Found strong / weak / linked</dt>
          <dd>
            {count(m2.found_strong)} / {count(m2.found_weak)} / {count(m2.found_linked)}
          </dd>
          <dt className="text-ink-subtle">False matches</dt>
          <dd>
            {count(m2.false_matches)} of {count(m2.reviewed_matches)} reviewed
          </dd>
          <dt className="text-ink-subtle">Unreviewed</dt>
          <dd>
            {count(m2.unreviewed_suggestions)} suggestions · {count(m2.unreviewed_matches)} matches
          </dd>
        </dl>
        <div className="flex flex-col gap-2 text-body-s">
          {recalls.map((recall) => (
            <div key={recall.label}>
              <div className="flex justify-between gap-2">
                <span className="text-ink-muted">Recall {recall.label}</span>
                <span>{rate(recall.value)}</span>
              </div>
              <Bar
                part={recall.value ?? 0}
                whole={1}
                tone={(recall.value ?? 0) >= m2.recall_target ? "ok" : "warn"}
                label={`Recall ${recall.label}`}
              />
            </div>
          ))}
          <div className="flex justify-between gap-2">
            <span className="text-ink-muted">False-match rate (limit {rate(m2.false_match_limit)})</span>
            <span className={falseRate !== null && falseRate > m2.false_match_limit ? "font-semibold text-critical-ink" : ""}>
              {rate(falseRate)}
            </span>
          </div>
          <p className="text-caption text-ink-subtle">Recall target {rate(m2.recall_target)} (strong + weak + linked).</p>
        </div>
      </div>
    </Section>
  );
}

function SitesTable({ sites }: { sites: SiteStats[] }) {
  return (
    <Section title="Enabled sites" aside={<span className="text-caption text-ink-subtle">tasks and media “1 h” = finished in the last hour</span>}>
      {sites.length === 0 ? (
        <p className="text-body-s text-ink-muted">No enabled sites.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-body-s">
            <thead className="text-left text-caption text-ink-subtle">
              <tr className="border-b border-hairline">
                <th className="py-1.5 pr-3 font-medium">Site</th>
                <th className="py-1.5 pr-3 font-medium">Pages (new 24 h)</th>
                <th className="py-1.5 pr-3 font-medium">Tasks pending · done 1 h · failed 1 h</th>
                <th className="py-1.5 pr-3 font-medium">Media pending · done 1 h · skipped</th>
                <th className="py-1.5 pr-3 font-medium">Playable · removed · unknown</th>
                <th className="py-1.5 font-medium">Requests 24 h / budget</th>
              </tr>
            </thead>
            <tbody>
              {sites.map((site) => (
                <tr key={site.id} className="border-b border-hairline align-top last:border-0">
                  <td className="py-2 pr-3 font-medium">{site.domain}</td>
                  <td className="py-2 pr-3 tabular-nums">
                    {count(site.pages_total)} <span className="text-ink-subtle">(+{count(site.pages_new_24h)})</span>
                  </td>
                  <td className="py-2 pr-3 tabular-nums">
                    {count(site.tasks_pending)} · {count(site.tasks_done_1h)} ·{" "}
                    <span className={site.tasks_failed_1h > 0 ? "font-semibold text-critical-ink" : ""}>{count(site.tasks_failed_1h)}</span>
                  </td>
                  <td className="py-2 pr-3 tabular-nums">
                    {count(site.media_pending)} · {count(site.media_done_1h)} · {count(site.media_skipped)}
                  </td>
                  <td className="py-2 pr-3 tabular-nums">
                    <div>
                      {count(site.availability.playable)} · {count(site.availability.removed)} · {count(site.availability.unknown)}
                    </div>
                    <Bar part={site.availability.playable} whole={site.pages_total} tone="ok" label={`${site.domain} playable share`} />
                  </td>
                  <td className="py-2 tabular-nums">
                    <div>
                      {count(site.requests_24h)} / {count(site.daily_request_budget)}
                    </div>
                    <Bar
                      part={site.requests_24h}
                      whole={site.daily_request_budget}
                      tone={budgetTone(site.requests_24h, site.daily_request_budget)}
                      label={`${site.domain} request budget used`}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

const JOB_STATUS: Record<string, string> = {
  ok: "bg-positive-quiet text-positive-ink",
  failed: "bg-critical-quiet text-critical-ink",
  skipped: "bg-surface-300 text-ink",
};

function JobsTable({
  jobs,
  durations,
  now,
}: {
  jobs: Job[];
  durations: Resources["scheduler"];
  now: Date;
}) {
  const durationByName = new Map(durations.map((job) => [job.name, job.last_duration_s]));
  return (
    <Section title="Scheduler">
      {jobs.length === 0 ? (
        <p className="text-body-s text-ink-muted">No job has run yet.</p>
      ) : (
        <table className="w-full text-body-s">
          <thead className="text-left text-caption text-ink-subtle">
            <tr className="border-b border-hairline">
              <th className="py-1.5 pr-3 font-medium">Job</th>
              <th className="py-1.5 pr-3 font-medium">Last status</th>
              <th className="py-1.5 pr-3 font-medium">Finished</th>
              <th className="py-1.5 pr-3 font-medium">Duration</th>
              <th className="py-1.5 font-medium">Summary</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.name} className="border-b border-hairline align-top last:border-0">
                <td className="py-2 pr-3 font-mono text-mono-s">{job.name}</td>
                <td className="py-2 pr-3">
                  <span className={`rounded-sm px-2 py-0.5 text-caption font-semibold ${JOB_STATUS[job.last_status] ?? "bg-surface-200 text-ink-muted"}`}>
                    {job.last_status || "never"}
                  </span>
                </td>
                <td className="py-2 pr-3 whitespace-nowrap" title={dateTime(job.last_finished_at)}>
                  {ago(job.last_finished_at, now)}
                </td>
                <td className="py-2 pr-3 tabular-nums whitespace-nowrap">
                  {durationLabel(durationByName.get(job.name) ?? null)}
                </td>
                <td className="py-2 text-caption break-words whitespace-pre-wrap text-ink-muted">{job.summary || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}

function ResourcesSection({ resources, now }: { resources: Resources; now: Date }) {
  const { host, database, queue, workers, runtime_lanes: runtimeLanes } = resources;
  const memoryUsedTone = host.memory ? budgetTone(host.memory.used_bytes, host.memory.total_bytes) : "ok";
  const diskTone = host.disk ? budgetTone(host.disk.used_bytes, host.disk.total_bytes) : "ok";
  const queueTone: Tone = queue.stuck_running > 0 || queue.paused_sites.length > 0 ? "warn" : "ok";
  return (
    <Section title="System resources" aside={<span className="text-caption text-ink-subtle">this development server, not production</span>}>
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <div>
          <div className="text-caption text-ink-subtle">Load average (1m / 5m / 15m), {count(host.cpu_count)} cores</div>
          <div className="tabular-nums">
            {[host.load_average_1m, host.load_average_5m, host.load_average_15m]
              .map((value) => (value === null ? "—" : value.toFixed(2)))
              .join(" / ")}
          </div>
          <Bar
            part={host.load_average_1m ?? 0}
            whole={host.cpu_count ?? 1}
            tone={loadTone(host.load_average_1m, host.cpu_count)}
            label="Load average (1 minute)"
          />
        </div>
        <div>
          <div className="text-caption text-ink-subtle">RAM</div>
          {host.memory ? (
            <>
              <div className="tabular-nums">
                {bytesLabel(host.memory.used_bytes)} / {bytesLabel(host.memory.total_bytes)}
              </div>
              <Bar part={host.memory.used_bytes} whole={host.memory.total_bytes} tone={memoryUsedTone} label="RAM used" />
            </>
          ) : (
            <div className="text-ink-subtle">not readable from here</div>
          )}
        </div>
        <div>
          <div className="text-caption text-ink-subtle">Disk (backend volume)</div>
          {host.disk ? (
            <>
              <div className="tabular-nums">
                {bytesLabel(host.disk.used_bytes)} / {bytesLabel(host.disk.total_bytes)}
              </div>
              <Bar part={host.disk.used_bytes} whole={host.disk.total_bytes} tone={diskTone} label="Disk used" />
            </>
          ) : (
            <div className="text-ink-subtle">not readable from here</div>
          )}
        </div>
        <div>
          <div className="text-caption text-ink-subtle">Postgres database</div>
          <div className="tabular-nums">{bytesLabel(database.size_bytes)}</div>
          <div className="text-caption text-ink-subtle">{database.tables.length} tables listed below</div>
        </div>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div>
          <h3 className="mb-1 text-body-s font-semibold">Largest tables</h3>
          <table className="w-full text-body-s">
            <tbody>
              {database.tables.map((table) => (
                <tr key={table.name} className="border-b border-hairline last:border-0">
                  <td className="py-1 pr-3 font-mono text-caption">{table.name}</td>
                  <td className="py-1 pr-3 tabular-nums">{bytesLabel(table.total_bytes)}</td>
                  <td className="py-1 tabular-nums text-ink-subtle">~{count(table.approx_rows)} rows</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div>
          <h3 className="mb-1 text-body-s font-semibold">Crawl queue</h3>
          <div className={`text-body-s ${queueTone === "warn" ? "font-semibold text-caution-ink" : ""}`}>
            {count(queue.pending_total)} pending · oldest {durationLabel(queue.oldest_pending_seconds)} old
            {queue.stuck_running > 0 ? ` · ${count(queue.stuck_running)} stuck running` : ""}
          </div>
          {queue.paused_sites.length === 0 ? (
            <p className="mt-1 text-caption text-ink-subtle">No site is paused.</p>
          ) : (
            <ul className="mt-1 text-caption text-caution-ink">
              {queue.paused_sites.map((paused) => (
                <li key={paused.domain}>
                  {paused.domain}: paused until {dateTime(paused.paused_until)} ({count(paused.pending)} pending) —{" "}
                  {paused.pause_reason || "no reason recorded"}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div>
          <h3 className="mb-1 text-body-s font-semibold">Per-site workers</h3>
          <div className="flex flex-wrap gap-2 text-caption">
            {workers.map((worker) => (
              <span key={worker.domain} className="rounded-sm border border-hairline px-2 py-1">
                {worker.domain}: crawler <StatusDot alive={worker.crawler_alive} /> · media{" "}
                <StatusDot alive={worker.media_alive} />
              </span>
            ))}
          </div>
        </div>
        <div>
          <h3 className="mb-1 text-body-s font-semibold">Crawl runtime lanes</h3>
          <ul className="text-caption">
            {runtimeLanes.map((lane) => (
              <li key={lane.lane}>
                {lane.lane}: <StatusDot alive={lane.alive} />{" "}
                {lane.alive ? `since ${ago(lane.since, now)}` : "not running"} · sites: {lane.sites.join(", ") || "none configured"}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Section>
  );
}

function StatusDot({ alive }: { alive: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${alive ? "bg-positive-ink" : "bg-hairline-strong"}`}
      role="img"
      aria-label={alive ? "alive" : "not running"}
    />
  );
}
