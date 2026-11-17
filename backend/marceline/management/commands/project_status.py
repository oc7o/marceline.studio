"""`make status`: the current state of this project, derived from its sources of truth.

**Read-only, guaranteed.** This command writes nothing: no model is saved, no file is created or
touched, no request leaves the machine except the two `gh` reads of section 5 (which the
`--no-github` flag removes). Every database access below is a `SELECT`, every subprocess is a
read-only `git`, `docker inspect` or `gh` invocation, and nothing is cached to disk. That
guarantee is what makes it safe to run at the start of every session and after every compaction,
which is what it is for (issue #79, and the CLAUDE.md rule "Prove it before reporting it").

It **derives**: it reads no file a human is expected to keep up to date. The scheduler's jobs come
from `marceline.scheduler.JOBS` and their state from `core.JobRun`; the crawl budgets from the
same `catalog.request_accounting` functions the crawler itself is refused by; the running
containers' feature flags from `docker inspect`, not from `.env`, because **a container keeps the
environment it was created with** and the two can disagree for days; the branch list from git; the
issues from GitHub; the answered legal requests from which files exist. A status file would rot;
this cannot.

Every section degrades on its own to a single "unavailable" line, so the command still answers
with docker down, the database down and no network.

It prints no secret's value — a variable is `set`, `EMPTY` or `not passed` — and no piracy URL or
pirated title: sites appear as domains, findings as counts.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import DatabaseError
from django.db.models import Sum
from django.utils import timezone

from accounts import invites as customer_invites
from accounts import onboarding
from catalog import policy_audit, robots_policy
from catalog.catalogue_size import alert_percent, over_threshold
from catalog.models import RequestCount, Site, SiteCatalogueSize
from catalog.request_accounting import (
    MEDIA_BUDGET,
    PAGE_BUDGET,
    over_budget_site_ids,
    window_start,
)
from core.log_scrub import scrub_urls
from core.models import JobRun
from index import retention
from marceline import scheduler
from marceline.project_status import (
    compose_env_names,
    compose_service_names,
    env_gaps,
    flag_value,
    format_age,
    truncate,
    wrap,
)

#: The window `make status` reports policy switch changes over (tripwire T4, issue #106).
POLICY_CHANGE_DAYS = 7

REPO_ROOT = Path(__file__).resolve().parents[4]
#: The branch everything is merged into; section 1's "unmerged" is measured against it.
INTEGRATION_BRANCH = "main"
#: Branches listed by name before the rest are summarised (`--branches` prints them all).
#: The header always gives the true total, so a busy day shortens the list, never hides it.
BRANCH_LIMIT = 8
#: Recently updated issues listed by name.
ISSUE_LIMIT = 10
#: Labels that mean "this is waiting for the operator, not for work" (docs/workflow.md).
DECISION_LABELS = ("needs:operator", "type:decision", "needs:lawyer")
#: The compose project every checkout of this repository shares (infra/compose.yaml `name:`).
COMPOSE_PROJECT = "marceline"

#: The switches whose real, per-container value has to be visible. Both bugs the docstring names
#: were a flag believed on that no process ever received, so an absent variable is reported as
#: loudly as a false one.
WATCHED_FLAGS = (
    "TARGETED_INDEXING_ENABLED",
    "PAGE_RETENTION_ENABLED",
    "SAFETY_RETENTION_ENABLED",
    "RESEARCH_RETENTION_ENABLED",
    "BROWSER_CAPTURE_ENABLED",
    "MATCHING_METADATA_CANDIDATES_ENABLED",
    "MATCHING_EMBEDDINGS_ENABLED",
    "MATCHING_REFERENCE_CROP_GRID",
    "CACHE_PURGE_OUTBOX",
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    # Tripwire T2 (issue #105): detection is on by default, the automatic pause is not. Both have
    # to be visible per container, because only the processes that refresh robots.txt act on them.
    "ROBOTS_RESERVATION_DETECTION_ENABLED",
    "ROBOTS_RESERVATION_PAUSE_ENABLED",
)

GIT_TIMEOUT_S = 10
DOCKER_TIMEOUT_S = 15
GH_TIMEOUT_S = 25


@dataclass(frozen=True)
class Container:
    service: str
    image: str
    started_at: datetime | None
    env: dict[str, str]


def _run(args: Sequence[str], timeout: int) -> str | None:
    """Run a read-only command; `None` when it is missing, fails or does not answer in time."""
    try:
        done = subprocess.run(  # fixed argument lists, never a shell
            list(args), capture_output=True, text=True, timeout=timeout, cwd=REPO_ROOT
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _git(*args: str) -> str | None:
    return _run(["git", "-C", str(REPO_ROOT), *args], GIT_TIMEOUT_S)


def _age_since(moment: datetime | None, now: datetime) -> str:
    return "never" if moment is None else format_age((now - moment).total_seconds())


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


class Command(BaseCommand):
    help = (
        "The project's current state, derived from git, docker, the database, GitHub and the "
        "legal documents. Read-only: it writes nothing. Run it after every compaction."
    )
    # No system checks and no migration check: this command has to answer with the database down,
    # and it is the one command that must never be the reason a session cannot get its bearings.
    # (RUF012 wants a ClassVar here; mypy refuses one, because BaseCommand declares it as an
    # instance variable. Django's own commands write it exactly like this.)
    requires_system_checks: list[str] = []  # noqa: RUF012
    requires_migrations_checks = False
    #: Set on the first `gh` read; absent until then, which is what `_open_issues` tests.
    _issues_cache: list[dict[str, Any]] | None

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--branches", action="store_true", help="List every unmerged branch, not the newest."
        )
        parser.add_argument(
            "--gaps", action="store_true", help="Name every settings↔compose gap, not the silent."
        )
        parser.add_argument("--no-github", action="store_true", help="Skip the two `gh` reads.")

    def handle(self, *args: Any, **options: Any) -> None:
        now = timezone.now()
        self.stdout.write(f"marceline status {now:%Y-%m-%d %H:%M} UTC · derived, read-only")
        self._repository(now, full=options["branches"])
        self._running(now, full=options["gaps"])
        self._scheduler(now)
        self._crawl(now)
        self._invites(now)
        self._issues(skip=options["no_github"])
        self._legal()
        self._decisions(skip=options["no_github"])

    # --- 1. repository --------------------------------------------------------------------------

    def _repository(self, now: datetime, *, full: bool) -> None:
        self.stdout.write("")
        head = _git("rev-parse", "--abbrev-ref", "HEAD")
        if head is None:
            self.stdout.write("repo: unavailable (not a git checkout, or git failed)")
            return
        branch = head.strip()
        dirty = _git("status", "--porcelain")
        changed = len([line for line in (dirty or "").splitlines() if line.strip()])
        state = f"{changed} file(s) modified" if changed else "clean"
        self.stdout.write(f"repo: {branch} · {state} · {self._tracking()}")
        if last := _git("log", "-1", "--format=%h %cs %s"):
            self.stdout.write(f"  head {truncate(last.strip(), 84)}")
        self._unmerged(now, full=full)

    def _tracking(self) -> str:
        """Ahead/behind against the local remote-tracking ref. No fetch: that would be a write."""
        upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        if upstream is None:
            return "no upstream"
        ref = upstream.strip()
        counts = _git("rev-list", "--left-right", "--count", f"{ref}...HEAD")
        if counts is None:
            return f"{ref} (uncomparable)"
        behind, _, ahead = counts.strip().partition("\t")
        return f"{ahead} ahead / {behind} behind {ref} (no fetch)"

    def _unmerged(self, now: datetime, *, full: bool) -> None:
        listed = _git(
            "for-each-ref",
            "--sort=-committerdate",
            f"--no-merged={INTEGRATION_BRANCH}",
            "--format=%(refname:short)%09%(committerdate:unix)",
            "refs/heads",
        )
        if listed is None:
            self.stdout.write(f"  unmerged into {INTEGRATION_BRANCH}: unavailable")
            return
        rows = [line.split("\t") for line in listed.splitlines() if "\t" in line]
        if not rows:
            self.stdout.write(f"  unmerged into {INTEGRATION_BRANCH}: none")
            return
        worktrees = self._worktree_branches()
        shown = rows if full else rows[:BRANCH_LIMIT]
        self.stdout.write(
            f"  unmerged into {INTEGRATION_BRANCH}: {len(rows)} branch(es), newest first"
        )
        for name, stamp in shown:
            ahead = _git("rev-list", "--count", f"{INTEGRATION_BRANCH}..{name}")
            age = format_age(now.timestamp() - int(stamp))
            mark = " [worktree]" if name in worktrees else ""
            self.stdout.write(f"    {name:<34} +{(ahead or '?').strip():<4} {age:<5}{mark}")
        if len(rows) > len(shown):
            self.stdout.write(f"    … {len(rows) - len(shown)} older (--branches for all)")

    def _worktree_branches(self) -> set[str]:
        listed = _git("worktree", "list", "--porcelain")
        return {
            line.removeprefix("branch refs/heads/").strip()
            for line in (listed or "").splitlines()
            if line.startswith("branch refs/heads/")
        }

    # --- 2. running state -----------------------------------------------------------------------

    def _running(self, now: datetime, *, full: bool) -> None:
        self.stdout.write("")
        containers = self._containers()
        if containers is None:
            self.stdout.write("running: unavailable (docker not reachable)")
        elif not containers:
            self.stdout.write(f"running: nothing up in compose project `{COMPOSE_PROJECT}`")
        else:
            self._container_lines(containers, now)
            self._flag_lines(containers)
        self._gap_lines(full=full)

    def _containers(self) -> list[Container] | None:
        if shutil.which("docker") is None:
            return None
        listed = _run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={COMPOSE_PROJECT}",
                "--format",
                "{{.ID}}",
            ],
            DOCKER_TIMEOUT_S,
        )
        if listed is None:
            return None
        ids = listed.split()
        if not ids:
            return []
        raw = _run(["docker", "inspect", *ids], DOCKER_TIMEOUT_S)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return sorted(
            (_container(entry) for entry in payload), key=lambda container: container.service
        )

    def _container_lines(self, containers: Sequence[Container], now: datetime) -> None:
        parts = []
        for container in containers:
            # The image is only worth the width when it is not the one compose builds for the
            # service; `marceline-web` under `web` says nothing, `postgres:17` under `db` does.
            own = container.image == f"{COMPOSE_PROJECT}-{container.service}"
            image = "" if own else f" {container.image}"
            parts.append(f"{container.service}{image} {_age_since(container.started_at, now)}")
        for index, line in enumerate(wrap(" · ".join(parts), 74)):
            head = f"running ({len(containers)} up): " if index == 0 else " " * 4
            self.stdout.write(f"{head}{line}")
        self.stdout.write(self._worker_coverage({c.service for c in containers}))

    def _worker_coverage(self, up: set[str]) -> str:
        """How much of the per-site worker fleet is actually up, from compose's own service list."""
        defined = compose_service_names(_read(REPO_ROOT / "infra" / "compose.yaml"))
        counts = []
        for prefix in ("crawler-", "media-"):
            have = len({name for name in up if name.startswith(prefix)})
            total = len({name for name in defined if name.startswith(prefix)})
            counts.append(f"{have}/{total} {prefix}*")
        lanes = sorted(name for name in up if name in {"crawler-pages", "crawler-media"})
        runtime = f"runtime lanes up: {', '.join(lanes)}" if lanes else "runtime lanes: none up"
        return f"  containers: {' · '.join(counts)} · {runtime}"

    def _flag_lines(self, containers: Sequence[Container]) -> None:
        self.stdout.write("  flags as the running processes hold them (docker inspect, not .env):")
        for name in WATCHED_FLAGS:
            holders = [
                f"{container.service}={flag_value(container.env.get(name))}"
                for container in containers
                if name in container.env
            ]
            if holders:
                self.stdout.write(f"    {name:<38} {truncate(' '.join(holders), 50)}")
            else:
                self.stdout.write(f"    {name:<38} NOT PASSED to any running container")

    def _gap_lines(self, *, full: bool) -> None:
        compose_text = _read(REPO_ROOT / "infra" / "compose.yaml")
        dotenv = REPO_ROOT / ".env"
        gaps = env_gaps(
            _read(REPO_ROOT / "backend" / "marceline" / "settings.py"),
            compose_text,
            _read(dotenv),
            _read(REPO_ROOT / "infra" / "prod" / "compose.yaml"),
        )
        passed = len(compose_env_names(compose_text))
        self.stdout.write(
            f"  settings↔compose: {passed} key(s) passed, {gaps.total} setting(s) settings.py "
            f"reads reach no service"
        )
        if not dotenv.is_file():
            # Never report "no silent gaps" from an empty answer: a git worktree has no `.env`, so
            # the check that matters most would quietly pass exactly where it was not run.
            self.stdout.write(
                "    !! no .env here — run this in the main checkout for the silent-gap check"
            )
        elif gaps.silent:
            self.stdout.write(
                "    !! SET IN .env, PASSED TO NO CONTAINER (silently the settings.py default):"
            )
            self.stdout.write(f"       {' '.join(gaps.silent)}")
        else:
            self.stdout.write("    ok: every variable .env sets does reach a container")
        self.stdout.write(
            f"    of the rest, {len(gaps.prod_only)} passed by infra/prod/compose.yaml, "
            f"{len(gaps.default_only)} on their settings.py default"
        )
        if full:
            for label, names in (
                ("prod-only", gaps.prod_only),
                ("default-only", gaps.default_only),
            ):
                if names:
                    self.stdout.write(f"    {label}:")
                    for line in wrap(" ".join(names), 84):
                        self.stdout.write(f"      {line}")

    # --- 3. scheduler ---------------------------------------------------------------------------

    def _scheduler(self, now: datetime) -> None:
        self.stdout.write("")
        try:
            records = {record.name: record for record in JobRun.objects.all()}
        except DatabaseError:
            self.stdout.write("scheduler: unavailable (database not reachable)")
            return
        healthy: list[str] = []
        problems: list[str] = []
        for job in scheduler.JOBS:
            record = records.get(job.name)
            note = self._job_problem(job, record, now)
            age = _age_since(record.last_started_at if record else None, now)
            if note is None:
                healthy.append(f"{job.name} {age}")
            else:
                problems.append(f"    !! {job.name:<24} {note}")
        self.stdout.write(
            f"scheduler: {len(scheduler.JOBS)} jobs · {len(healthy)} ok · {len(problems)} "
            f"needing a look"
        )
        for line in problems:
            self.stdout.write(line)
        for index, line in enumerate(wrap(f"ok (age of last run): {', '.join(healthy)}", 86)):
            self.stdout.write(("    " if index == 0 else "       ") + line)

    def _job_problem(self, job: scheduler.Job, record: JobRun | None, now: datetime) -> str | None:
        """Why this job needs a look, or `None`. Skipped, failed, never run or long overdue."""
        if record is None or record.last_started_at is None:
            return "never run"
        age = (now - record.last_started_at).total_seconds()
        if record.last_status in (JobRun.Status.SKIPPED, JobRun.Status.FAILED):
            reason = truncate(scrub_urls(record.last_message), 60)
            return f"{record.last_status} {format_age(age)} ago: {reason}"
        try:
            interval = job.interval().total_seconds()
        except ValueError as error:
            return f"misconfigured interval ({error})"
        if age > 2 * interval:
            return f"last ran {format_age(age)} ago, interval {format_age(interval)} (overdue)"
        return None

    # --- 4. crawl -------------------------------------------------------------------------------

    def _crawl(self, now: datetime) -> None:
        self.stdout.write("")
        try:
            enabled = Site.objects.filter(enabled=True).count()
            page_spent = self._domains(over_budget_site_ids(now, budget=PAGE_BUDGET))
            media_spent = self._domains(over_budget_site_ids(now, budget=MEDIA_BUDGET))
            client_errors = (
                RequestCount.objects.filter(hour__gte=window_start(now), outcome="4xx").aggregate(
                    total=Sum("requests")
                )["total"]
                or 0
            )
        except DatabaseError:
            self.stdout.write("crawl: unavailable (database not reachable)")
            return
        self.stdout.write(
            f"crawl: {enabled} site(s) enabled · {client_errors} 4xx in the last 24 h"
        )
        for label, domains in (("page budget", page_spent), ("media budget", media_spent)):
            if domains:
                listed = truncate(" ".join(domains), 80)
                self.stdout.write(f"    at 100 % of {label} (24 h): {listed}")
        if not page_spent and not media_spent:
            self.stdout.write("    no site is at its page or media budget")
        self.stdout.write(f"    {self._catalogue_share()}")
        self.stdout.write(f"    {self._page_scrub()}")
        self._tripwires(now)

    def _page_scrub(self) -> str:
        """One line: deactivated pages that still hold the text a deactivation deletes (#148).

        A page deactivated as untargeted has its title, tags and performer credits deleted. This
        is the number that says whether that is true right now — it must be 0. One aggregate, no
        write.
        """
        try:
            gap = retention.scrub_gap()
        except DatabaseError:
            return "page scrub: unavailable"
        if gap.clean:
            return (
                f"page scrub: 0 of {gap.inactive} deactivated page(s) still hold title, tags or "
                "credits — as V06 and crawl-position.md § 2 state"
            )
        return (
            f"page scrub: {gap.holding_text} of {gap.inactive} deactivated page(s) STILL HOLD "
            f"title, tags or credits ({gap.safety_flagged} safety-flagged, left untouched by "
            "design) — `manage.py scrub_inactive_pages` (dry run first), issue #148"
        )

    def _catalogue_share(self) -> str:
        """One line: how many sites have a known catalogue total, and which are over (issue #107).

        Derived, like everything else here: `catalog.SiteCatalogueSize` and the counts
        `index.catalogue_share` stamped into it. No request, no write.
        """
        try:
            records = list(SiteCatalogueSize.objects.select_related("site"))
        except DatabaseError:
            return "catalogue share: unavailable"
        enabled = Site.objects.filter(enabled=True).count()
        over = sorted(record.site.domain for record in records if over_threshold(record))
        limit = alert_percent()
        if not records:
            return (
                f"catalogue share: no total recorded for any of {enabled} enabled site(s) — "
                "no share can be computed (manage.py catalogue_size)"
            )
        if not over:
            return (
                f"catalogue share: {len(records)} of {enabled} site(s) have a total; "
                f"none over {limit:.0f} % ever indexed"
            )
        return (
            f"catalogue share: {len(records)} of {enabled} site(s) have a total; "
            f"over {limit:.0f} %: {truncate(' '.join(over), 80)}"
        )

    def _invites(self, now: datetime) -> None:
        """Onboarding by invite (issue #115, ADR 0052): how many links are out, and can they work.

        Two switches decide whether an issued link does anything at all, and both fail *silently*:
        with `ONBOARDING_INVITES_ENABLED` off every route answers the same neutral 404 an expired
        token gets, and with no published document version the accept step refuses. So the line
        says the counts **and** both switches — counts alone would read as "nobody has redeemed
        theirs yet" in exactly the case where nobody could.
        """
        try:
            counts = customer_invites.counts(now=now)
        except DatabaseError:
            self.stdout.write("")
            self.stdout.write("invites: unavailable (database not reachable)")
            return
        switch = "on" if onboarding.invites_enabled() else "OFF"
        documents = "published" if onboarding.documents_available() else "NOT published"
        self.stdout.write("")
        self.stdout.write(
            f"invites: {counts['open']} open · {counts['redeemed']} redeemed · "
            f"{counts['expired']} expired · {counts['revoked']} revoked"
        )
        self.stdout.write(
            f"    ONBOARDING_INVITES_ENABLED {switch} · documents {documents} "
            f"(an invite cannot be completed while either is missing)"
        )

    def _tripwires(self, now: datetime) -> None:
        """Tripwires: reservations, policy switches."""
        try:
            reservations = robots_policy.counts(now)
            changes = policy_audit.recent_changes(now, days=POLICY_CHANGE_DAYS)
        except DatabaseError:
            self.stdout.write("    tripwires: unavailable (database not reachable)")
            return
        pause = "on" if robots_policy.pause_enabled() else "off"
        self.stdout.write(
            f"    robots reservations (enabled sites): naming us {reservations.naming_us} · "
            f"`*` {reservations.wildcard} · third-party AI {reservations.third_party} · "
            f"paused for review {reservations.paused} (auto-pause {pause})"
        )
        self.stdout.write(
            f"    policy switches changed in the last {POLICY_CHANGE_DAYS} d: {changes.total}"
        )
        for row in changes.rows:
            self.stdout.write(f"       {truncate(policy_audit.describe(row), 96)}")

    def _domains(self, site_ids: Iterable[int]) -> list[str]:
        ids = list(site_ids)
        if not ids:
            return []
        return sorted(Site.objects.filter(pk__in=ids).values_list("domain", flat=True))

    # --- 5. backlog and issues ------------------------------------------------------------------

    def _issues(self, *, skip: bool) -> None:
        self.stdout.write("")
        if skip:
            self.stdout.write("github: skipped (--no-github)")
            return
        issues = self._open_issues()
        prs = self._gh_json(
            ["pr", "list", "--state", "open", "--limit", "50", "--json"], "number,title,updatedAt"
        )
        if issues is None:
            self.stdout.write("github: unreachable (no network, or `gh` not authenticated)")
            return
        open_prs = "unavailable" if prs is None else str(len(prs))
        self.stdout.write(f"github: {len(issues)} open issue(s) · {open_prs} open PR(s)")
        now = datetime.now(UTC)
        for issue in self._newest(issues, ISSUE_LIMIT):
            age = _age_since(_parse_time(issue.get("updatedAt")), now)
            self.stdout.write(
                f"    #{issue['number']:<4} {age:<5} {truncate(str(issue.get('title', '')), 68)}"
            )
        for pull in self._newest(prs or [], 5):
            self.stdout.write(
                f"    PR #{pull['number']:<3} {truncate(str(pull.get('title', '')), 68)}"
            )

    def _open_issues(self) -> list[dict[str, Any]] | None:
        """The open issues, fetched at most once: sections 5 and 7 read the same answer."""
        if not hasattr(self, "_issues_cache"):
            self._issues_cache = self._gh_json(
                ["issue", "list", "--state", "open", "--limit", "200", "--json"],
                "number,title,updatedAt,labels",
            )
        return self._issues_cache

    def _newest(self, rows: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda row: str(row.get("updatedAt", "")), reverse=True)[:limit]

    def _gh_json(self, args: Sequence[str], fields: str) -> list[dict[str, Any]] | None:
        binary = self._gh_binary()
        if binary is None:
            return None
        raw = _run([binary, *args, fields], GH_TIMEOUT_S)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, list) else None

    def _gh_binary(self) -> str | None:
        local = Path.home() / ".local" / "bin" / "gh"
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
        return shutil.which("gh")

    # --- 6. legal -------------------------------------------------------------------------------

    def _legal(self) -> None:
        self.stdout.write("")
        legal = REPO_ROOT / "docs" / "legal"
        requests = sorted(legal.glob("request-*.md")) if legal.is_dir() else []
        if not requests:
            self.stdout.write("legal: unavailable (docs/legal/ has no request-*.md)")
            return
        answered = {path.name.split("-", 1)[0] for path in (legal / "answers").glob("*.md")}
        waiting = [
            path.name for path in requests if path.name.removeprefix("request-")[:2] not in answered
        ]
        self.stdout.write(
            f"legal: {len(requests) - len(waiting)} of {len(requests)} counsel request(s) answered"
        )
        if waiting:
            self.stdout.write(f"    unanswered: {' '.join(waiting)}")

    # --- 7. operator decisions ------------------------------------------------------------------

    def _decisions(self, *, skip: bool) -> None:
        self.stdout.write("")
        if skip:
            self.stdout.write("operator decisions: skipped (--no-github)")
            return
        issues = self._open_issues()
        if issues is None:
            self.stdout.write("operator decisions: github unreachable")
            return
        # The source of truth is the label set docs/workflow.md defines: `needs:operator` means a
        # decision by the founder is required before work starts. There is no tracking file, and
        # this command does not invent one.
        waiting = {issue["number"] for issue in issues if _labels(issue) & set(DECISION_LABELS)}
        self.stdout.write(
            f"operator decisions (open issues, docs/workflow.md labels): {len(waiting)} waiting"
        )
        for label in DECISION_LABELS:
            numbers = sorted(issue["number"] for issue in issues if label in _labels(issue))
            listed = " ".join(f"#{number}" for number in numbers) or "none"
            for index, line in enumerate(wrap(listed, 80)):
                head = f"{label:<15}{len(numbers):>3}:" if index == 0 else " " * 19
                self.stdout.write(f"    {head} {line}")
        # Only `type:decision` is a decision as such; `needs:operator`/`needs:lawyer` mark work
        # that waits on one. So those get their titles, and the rest stay numbers to look up.
        for issue in sorted(issues, key=lambda row: int(row["number"])):
            if "type:decision" in _labels(issue):
                title = truncate(str(issue.get("title", "")), 66)
                self.stdout.write(f"    #{issue['number']:<4} {title}")


def _container(entry: dict[str, Any]) -> Container:
    config = entry.get("Config") or {}
    labels = config.get("Labels") or {}
    env = {}
    for item in config.get("Env") or []:
        name, _, value = str(item).partition("=")
        env[name] = value
    return Container(
        service=str(labels.get("com.docker.compose.service") or entry.get("Name", "?").lstrip("/")),
        image=str(config.get("Image", "?")),
        started_at=_parse_time((entry.get("State") or {}).get("StartedAt")),
        env=env,
    )


def _parse_time(raw: object) -> datetime | None:
    """Parse an RFC 3339 timestamp; docker's has nanoseconds, which `fromisoformat` rejects."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        fraction, sign, offset = _split_offset(tail)
        text = f"{head}.{fraction[:6]}{sign}{offset}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _split_offset(tail: str) -> tuple[str, str, str]:
    for sign in ("+", "-"):
        if sign in tail:
            fraction, _, offset = tail.partition(sign)
            return fraction, sign, offset
    return tail, "", ""


def _labels(issue: dict[str, Any]) -> set[str]:
    return {str(label.get("name", "")) for label in issue.get("labels") or []}
