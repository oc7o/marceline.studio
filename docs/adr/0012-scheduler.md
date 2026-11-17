# 0012 — Scheduler process

Status: accepted (2026-09-15)

## Context

Seeds, availability re-checks, clustering and reference matching were run by hand (or cron).
Crawlers (`crawl_worker --site X`) and the media worker are separate long-running processes that
drain table queues politely; the periodic batch work needs one owner that never outpaces them.

## Decision

One `manage.py scheduler` process (`--once` for cron/tests, `--only JOB`). It lives in the
project package: only `marceline` may import both `crawler` and `matching`, so the project is
added to `INSTALLED_APPS` to hold the management command (no models).

- **Single instance:** a session-level Postgres advisory lock; a second scheduler logs and exits.
- **Declarative jobs:** a tuple of `Job(name, default_hours, run, guard)` in
  `marceline/scheduler.py`, run in declaration order each pass (poll every 60 s). A job is due
  when `interval` has passed since its last start; intervals are overridable with
  `SCHEDULER_<JOB>_HOURS` (positive integer hours).
- **State in `core.JobRun`** (one row per job: last start/finish/success, status ok/failed/skipped,
  short message; read-only admin). The start is saved before running, so a crashing job waits a
  full interval instead of looping on restart.
- **Run summaries:** capture full command output at DEBUG; omit indented/detail lines from
  summaries. Reference matching starts with totals (references, pages matched, strong, weak),
  followed by each reference summary; page totals count matches per reference, not unique pages.
  Other jobs join summary lines with `; `. Store and log completion summaries at INFO with a
  2,000-character limit and a trailing `…` when truncated.
- **Failure isolation:** exceptions are logged and recorded; other jobs still run; retry at the
  next interval.
- Jobs call existing management commands via `call_command`:

| Job | Default | Guard | Runs |
|---|---|---|---|
| `crawl_seeds` | 24 h | per enabled site: skip while > 300 pending CrawlTasks | `crawl_seed --site X --refresh` |
| `crawl_recheck` | 24 h | same per-site backlog guard, except the never-checked batch below | `crawl_recheck --site X --older-than-hours 24` |
| `crawl_recheck_removed` | 168 h | none (bounded batch, low priority) | `crawl_recheck --site X --only-removed --older-than-hours 168 --limit 50 --priority 5` |
| `build_clusters` | 3 h | new FrameHash since last successful start | `build_clusters` |
| `build_common_frames` | 24 h | new FrameHash since last successful start | `build_common_frames` (after `build_clusters`) |
| `match_references` | 3 h | references exist; new FrameHash or Reference since last successful start | `match_references`, incremental (after `build_common_frames`) |

### Common frames and incremental matching (2026-09-15)

`build_common_frames` (ADR 0015) runs daily (`SCHEDULER_BUILD_COMMON_FRAMES_HOURS`) with the same
new-frame-hash guard as `build_clusters`. It is declared between `build_clusters` and
`match_references`: groups count clusters as one video, and common frames stop counting as match
evidence, so when both are due in one pass the groups are fresh before matching. Its summary joins
the command's top-level lines. The scheduler calls `match_references` without `--full`: each run
scans only page frames and references added since the previous scan, and the command falls back
to a full scan by itself when its settings fingerprint changed (e.g. after a common-frame rebuild
changed the group set). Operators run `match_references --full` by hand when needed.

### Never-checked batch (2026-09-15)

The backlog guard starved availability checks: 263 site03 and 221 site02 pages predating
availability tracking had never been checked, because those sites stayed above 300 pending tasks,
and reviewers saw dead "Post Not Found" links as `unknown`. When a site is over the threshold,
`crawl_recheck` therefore still runs `crawl_recheck --site X --never-checked --limit 100
--priority 35` (`NEVER_CHECKED_BATCH`, `crawler.services.RECHECK_PRIORITY`) instead of skipping the
site entirely. It is logged at INFO (`crawl_recheck: X over backlog, never-checked batch …`) and
appears in the run summary as `X: never-checked batch: Enqueued N task(s).`; the site is still
listed under `backlog skipped`. Pages that were checked before still wait for the backlog to drain.

`RECHECK_PRIORITY` (35) ranks above seeds (30) and ordinary details (≤ 20) and below details with
expiring media tokens (40). The batch adds at most 100 tasks per site per day; the crawler still
applies robots.txt, the per-host delay and the daily request budget. Reviewers can queue the same
kind of task for chosen pages from the moderation UI (ADR 0014).

### Removed pages (2026-09-17)

`crawl_recheck` skips removed pages, so a re-upload at a removed URL was never noticed and a
customer case stayed "handled" for ever (review of the customer app, M5). `crawl_recheck_removed`
runs weekly and queues, per enabled site, at most 50 removed **active** pages (ADR 0029) that were
not checked in the last 7 days, least recently checked first, at `DEEP_DETAIL_PRIORITY` (5): below
every ordinary detail page, above pagination chains. It has no backlog guard — the batch is small
and waits behind the backlog's details — and the crawler still applies robots.txt, the per-host
delay and the daily request budget. A playable answer turns the page back to `playable`.

## Consequences

No Celery/Redis/cron dependency; the scheduler needs only database access. Expired-media
recovery stays inside `crawl_worker`. A backlog-skipped site waits for the next 24 h pass.
"New rows" guards compare `created_at` with the last successful start, so a row inserted in a
transaction that commits after that start (e.g. a long `add_reference` import) is picked up only
when further frames arrive or the operator runs `match_references` by hand. Losing the database
connection releases the lock; the process then fails and is restarted by compose.
