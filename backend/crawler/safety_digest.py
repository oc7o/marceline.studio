"""One consolidated operator mail per day: safety, then crawler (2026-09-18, extended 2026-09-19).

The per-block alert of issue #40 (`crawler/alerts.py`) was measured too loud once the crawl runtime
started serving every site (ADR 0030): its ten-minute folding still produced **tens of mails** in
two days. Almost none of them needed an answer that hour — the large majority were `auto_block`
(non-consent wording) or `customer_release_queue` blocks, which have their own queues and their
own review step. So the operator got one digest instead — and then found three more individual
mails still landing on top of it: one for an incident-rule block (`crawler/alerts.py`), one per site for a
new/changed robots.txt TDM/AI reservation (`catalog/robots_alerts.py`), and one per adapter that
looks broken (`crawler/adapter_alerts.py`). All three now fold into **this** mail by default
instead (2026-09-19) — `core.operator_mail.OPERATOR_MAIL_IMMEDIATE` is the per-kind escape hatch
back to mailing on its own, off by default. Nothing about how a block, a reservation or a broken
adapter is *recorded* changes: only whether it also gets an mail of its own.

- **What it answers in fifteen seconds.** Safety: how many pages were blocked and whether that is
  more or less than the window before, the incident-rule blocks of this window by name (page id,
  site, category, rule, admin link — exactly the fields the old per-event mail carried), what is
  *waiting* for a human, and which sites and rules produced the blocks. Crawler: which adapters were
  newly reported broken, and which sites newly recorded a TDM/AI reservation (and whether it paused
  them). Customers: how many of their own data requests (ADR 0056, issue #133) are waiting and how
  old the oldest one is — the one deadline in this mail a statute sets. An empty section says so in
  one line rather than being silently missing.
- **When.** The scheduler job `safety_digest` runs once a day on or after `SAFETY_DIGEST_HOUR_UTC`
  (`marceline/scheduler.py`), so the mail is in the mailbox before the working day. The window is
  the period since the last successful run of that job, so a scheduler that was down for two days
  sends one mail covering both — never a gap, never the same event twice.
- **Every day sends one mail, even a fully quiet one (2026-09-19).** This is now the one mail that
  tells the operator the whole reporting pipeline is alive — a `safety_digest` that used to go quiet
  on a good day is indistinguishable from a `safety_digest` that stopped running at all. The body
  says so in its footer. The job still runs and still records itself in `core.JobRun` either way, so
  `make status` shows when it last ran and what it said.
- **A failed send never advances the window.** `send()` raises on a failed delivery (no
  `fail_silently`); the scheduler's `run_job` only stamps `last_succeeded_at` on success
  (`marceline/scheduler.py`), so the next run's window starts from the same point and nothing is
  lost — unlike the per-event alerts there is no crawl to protect here, so nothing here swallows an
  exception on purpose.

**Content rule (non-negotiable, docs/safety.md), identical to the individual mails it replaces.**
Page ids, site domains, safety categories, our own rule terms (`catalog.SafetyTerm`), counts and
admin links. Never a page URL, a title, extracted text, a performer name, a thumbnail or anything
about the media. The one query that reads pages asks for three columns (id, site domain, matched
rules) so that no page text can reach the renderer by accident. `catalog/catalogue_alerts.py`
(issue #107, the catalogue-share threshold) is a separate, much rarer mail (once per crossing) and
is deliberately not folded in here — it was not part of what the operator asked to consolidate.
"""

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from catalog import robots_policy, safety_rules
from catalog.models import SiteRobotsPolicy
from crawler import alerts
from crawler.models import AdapterHealth
from index.models import Page, SafetyIncident, SafetyRuleDecision

logger = logging.getLogger(__name__)

#: How many sites and rules the mail lists by name before the rest become one "and N more" line.
#: The header always carries the true total, so a busy day shortens the list, never hides it.
SITE_LINES = 8
RULE_LINES = 8
#: The window a first run (no previous successful run recorded) covers.
DEFAULT_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class DataRequests:
    """Customer data requests still open: how many, and the oldest one's age in whole days.

    A creator's own request for an export of their data or for its deletion (ADR 0056, issue #133)
    has a **statutory** answering deadline — one month, Art. 12(3) GDPR — and it is the only thing
    in this mail with one, which is exactly why it belongs here: nothing else would notice a request
    sitting in the admin for three weeks.

    Counts and days only, and it arrives from outside: the rows live in the project package, which
    `crawler` may not import (import-linter, "crawler depends only on catalog, index and core"), so
    `marceline/apps.py` installs `data_request_source` below at startup. Two integers is also the
    right thing to carry across that boundary — which organisation asked belongs in the admin, never
    in a mail.
    """

    waiting: int
    oldest_days: int | None


#: Installed by `marceline/apps.py` at startup (`marceline.deletion.requests.waiting`), left None
#: in a test that does not care. `collect` calls it; nothing here knows what is behind it.
data_request_source: Callable[[], DataRequests] | None = None


def load_data_requests() -> DataRequests | None:
    """What the project package says is waiting, or None when nothing installed a source.

    Never raises: a mail that failed to send because one section could not be counted would lose
    the whole report, and this is the section that exists so that nothing goes unseen.
    """
    source = data_request_source
    if source is None:
        return None
    try:
        return source()
    except Exception:  # pragma: no cover - defensive; a counting query has no expected failure
        logger.exception("could not count the open customer data requests")
        return None


@dataclass(frozen=True)
class Rule:
    """One matched safety rule: our own term list, never page content."""

    action: str
    category: str
    term: str

    @property
    def opens_incident(self) -> bool:
        return safety_rules.blocks(self.action) and (
            self.action not in safety_rules.NO_INCIDENT_BLOCK_ACTIONS
        )

    def label(self) -> str:
        action = self.action or "(unknown)"
        category = self.category or "(none)"
        return f'{action:<24}{category:<13}"{self.term}"'


@dataclass(frozen=True)
class Block:
    """One page that became blocked inside the window. Three facts, all of them ours."""

    page_id: int
    site_domain: str
    rules: tuple[Rule, ...]

    @property
    def incident_class(self) -> bool:
        """Blocked by a rule that opens a safety incident — the class the old alert singled out.

        Exactly `catalog.safety_rules.opens_incident` over the same matches, so this count and
        `alerts.mails_immediately` can never disagree about what would have been mailed at once. A
        page a human blocked without any rule match is therefore not in it.
        """
        return any(rule.opens_incident for rule in self.rules)

    def categories(self) -> str:
        return ", ".join(sorted({rule.category for rule in self.rules if rule.category})) or (
            "(none recorded)"
        )

    def terms(self) -> str:
        return ", ".join(sorted({rule.term for rule in self.rules if rule.term})) or (
            "(none recorded)"
        )


def parse_rules(matches: object) -> tuple[Rule, ...]:
    """The stored `Page.safety_matches` rows as rules, canonical actions. Pure."""
    if not isinstance(matches, list):
        return ()
    return tuple(
        Rule(
            action=safety_rules.canonical_action(row.get("action")),
            category=str(row.get("category") or ""),
            term=str(row.get("term") or ""),
        )
        for row in matches
        if isinstance(row, dict)
    )


@dataclass(frozen=True)
class Tally:
    """What one window's blocks amount to. Pure counts."""

    pages: int
    incident_pages: int
    by_site: tuple[tuple[str, int], ...]
    by_rule: tuple[tuple[Rule, int], ...]

    def site_count(self, domain: str) -> int:
        return dict(self.by_site).get(domain, 0)


def tally(blocks: Iterable[Block]) -> Tally:
    """Count pages per site and per rule. A page counts once per rule it matched. Pure."""
    sites: Counter[str] = Counter()
    rules: Counter[Rule] = Counter()
    pages = 0
    incident_pages = 0
    for block in blocks:
        pages += 1
        incident_pages += 1 if block.incident_class else 0
        sites[block.site_domain] += 1
        for rule in set(block.rules):
            rules[rule] += 1
    return Tally(
        pages=pages,
        incident_pages=incident_pages,
        by_site=tuple(sorted(sites.items(), key=lambda item: (-item[1], item[0]))),
        by_rule=tuple(sorted(rules.items(), key=lambda item: (-item[1], item[0].label()))),
    )


@dataclass(frozen=True)
class AdapterAlarm:
    """One site an adapter-health pass newly reported broken in this window.

    Exactly the fields `crawler/adapter_alerts.py`'s own mail carried: a domain and its counters-
    only detail line. Never a page id, a URL or any extracted text.
    """

    domain: str
    detail: str


@dataclass(frozen=True)
class TdmReservation:
    """One (site, host) whose robots.txt newly declared or changed a reservation in this window.

    Exactly the shape `catalog/robots_alerts.py`'s own mail carried, minus the token lists (the
    admin link is where those live): a site id, a host, which of the three reservation kinds it is,
    and whether it is currently paused for it.
    """

    site_id: int
    host: str
    names_us: bool
    names_wildcard: bool
    third_party_ai: bool
    paused: bool

    def kind(self) -> str:
        if self.names_us:
            return "names us"
        if self.names_wildcard:
            return "names `*` (already obeyed)"
        return "third-party AI/TDM tokens only"


@dataclass(frozen=True)
class Digest:
    """Everything the mail says. Built by `collect`, rendered by `subject` / `body`."""

    start: datetime
    end: datetime
    current: Tally
    previous: Tally
    incidents_open: int
    incidents_reported: int
    incidents_opened: int
    release_queue_waiting: int
    released: int
    queued: int
    staff_changes: int
    incident_blocks: tuple[Block, ...]
    adapter_alarms: tuple[AdapterAlarm, ...]
    tdm_reservations: tuple[TdmReservation, ...]
    #: Open customer data requests (ADR 0056). None when this machine could not count them, which
    #: reads as "not counted" in the mail and never as "none waiting".
    data_requests: DataRequests | None = None

    @property
    def delta(self) -> int:
        return self.current.pages - self.previous.pages


def _hours(start: datetime, end: datetime) -> str:
    return f"{(end - start).total_seconds() / 3600:.1f} h"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def subject(digest: Digest) -> str:
    """Counts and a date only — never a term, a site or anything about a page."""
    parts = [
        f"{digest.current.pages} blocked ({_signed(digest.delta)})",
        f"{digest.current.incident_pages} by an incident rule",
    ]
    waiting = digest.incidents_open + digest.release_queue_waiting
    parts.append(f"{waiting} waiting" if waiting else "nothing waiting")
    if digest.adapter_alarms:
        parts.append(f"{len(digest.adapter_alarms)} adapter alarm(s)")
    if digest.tdm_reservations:
        parts.append(f"{len(digest.tdm_reservations)} TDM reservation(s)")
    # The one deadline in this mail that a statute sets (Art. 12(3) GDPR), so it reaches the subject
    # line whenever there is one at all.
    if digest.data_requests is not None and digest.data_requests.waiting:
        # Not "… waiting": the safety part of this line already says "nothing waiting" about
        # incidents, and the two next to each other read as a contradiction.
        parts.append(f"{digest.data_requests.waiting} data request(s) open")
    return f"Marceline daily report {_stamp(digest.end)[:10]}: {', '.join(parts)}"


def _needs_a_look(digest: Digest) -> list[str]:
    lines: list[str] = []
    if digest.incidents_open or digest.incidents_reported:
        lines += [
            f"- {digest.incidents_open} safety incident(s) open, {digest.incidents_reported} "
            f"reported and not settled ({digest.incidents_opened} opened in this window)",
            f"  {alerts.admin_url('admin:index_safetyincident_changelist')}",
        ]
    if digest.release_queue_waiting:
        lines += [
            f"- {digest.release_queue_waiting} page(s) waiting in the customer release queue",
            f"  {alerts.admin_url('admin:index_page_changelist')}?safety_release__exact=queued",
        ]
    return ["Needs a look:", *lines] if lines else ["Needs a look: nothing is waiting."]


def _by_site(digest: Digest) -> list[str]:
    rows = digest.current.by_site
    lines = ["Blocked pages by site (the window before in brackets):"]
    for domain, count in rows[:SITE_LINES]:
        lines.append(f"  {domain:<24} {count:>5}  [{digest.previous.site_count(domain)}]")
    rest = rows[SITE_LINES:]
    if rest:
        lines.append(f"  and {len(rest)} more site(s): {sum(count for _, count in rest)}")
    return lines


def _by_rule(digest: Digest) -> list[str]:
    rows = digest.current.by_rule
    lines = ["Blocked pages by rule (action, category, term):"]
    for rule, count in rows[:RULE_LINES]:
        lines.append(f"  {rule.label():<52} {count:>5}")
    rest = rows[RULE_LINES:]
    if rest:
        lines.append(f"  and {len(rest)} more rule(s): {sum(count for _, count in rest)}")
    return lines


def _incident_block_lines(digest: Digest) -> list[str]:
    if not digest.incident_blocks:
        return ["Blocked by an incident rule in this window: none."]
    lines = [
        f"Blocked by an incident rule in this window ({len(digest.incident_blocks)}) — mailed "
        "individually only while OPERATOR_MAIL_IMMEDIATE names 'safety_incident':"
    ]
    for block in digest.incident_blocks:
        lines.append(
            f"- page {block.page_id} - site {block.site_domain} - category {block.categories()} "
            f'- term "{block.terms()}" '
            f"- {alerts.admin_url('admin:index_page_change', block.page_id)}"
        )
    return lines


def _adapter_alarm_lines(digest: Digest) -> list[str]:
    if not digest.adapter_alarms:
        return ["Adapter health: nothing to report."]
    lines = [f"{len(digest.adapter_alarms)} adapter(s) newly reported broken in this window:"]
    for alarm in digest.adapter_alarms:
        lines.append(f"- {alarm.domain}: {alarm.detail}")
    lines.append(f"  {alerts.admin_url('admin:crawler_adapterhealth_changelist')}")
    return lines


def _tdm_reservation_lines(digest: Digest) -> list[str]:
    if not digest.tdm_reservations:
        return ["TDM/AI reservations: nothing new in this window."]
    lines = [
        f"{len(digest.tdm_reservations)} site(s) with a new or changed robots.txt reservation:"
    ]
    for row in digest.tdm_reservations:
        paused = " - PAUSED pending review" if row.paused else ""
        lines.append(f"- site {row.site_id} host {row.host}: {row.kind()}{paused}")
    lines.append(f"  {alerts.admin_url('admin:catalog_siterobotspolicy_changelist')}")
    return lines


def _data_request_lines(digest: Digest) -> list[str]:
    """One line, and the admin link when there is something to work on (ADR 0056, issue #133).

    The wording says "waiting", never "overdue": the deadline is one month per request and this mail
    reports the oldest one's age so the operator can do that arithmetic themselves. A machine that
    could not count them says so — "0 waiting" would be a claim, and the wrong one.
    """
    requests = digest.data_requests
    if requests is None:
        return ["Data requests waiting: not counted on this machine."]
    if not requests.waiting:
        return ["Data requests waiting: 0."]
    oldest = "unknown" if requests.oldest_days is None else f"{requests.oldest_days} days"
    return [
        f"Data requests waiting: {requests.waiting} (oldest: {oldest}). One month to answer each "
        "(Art. 12(3) GDPR).",
        f"  {alerts.admin_url('admin:marceline_customerdatarequest_changelist')}",
    ]


def body(digest: Digest) -> str:
    """The mail. Pure: counts, domains, our own rule terms and admin links, nothing else."""
    lines = [
        f"Daily operator report for {_stamp(digest.start)} -> {_stamp(digest.end)} UTC "
        f"({_hours(digest.start, digest.end)}).",
        "",
        "== Safety ==",
        f"{digest.current.pages} page(s) newly blocked "
        f"({digest.previous.pages} in the window before, {_signed(digest.delta)}).",
        f"{digest.current.incident_pages} of them by a rule that opens a safety incident.",
        "",
        *_incident_block_lines(digest),
        "",
        *_needs_a_look(digest),
        "",
        *_by_site(digest),
        "",
        *_by_rule(digest),
        "",
        f"Also in this window: {digest.released} page(s) released for fingerprinting, "
        f"{digest.queued} queued for customer release, "
        f"{digest.staff_changes} staff status change(s).",
        "",
        "== Crawler ==",
        *_adapter_alarm_lines(digest),
        "",
        *_tdm_reservation_lines(digest),
        "",
        "== Customers ==",
        *_data_request_lines(digest),
        "",
        "This mail carries no page URL, title, extracted text or media on purpose "
        '(docs/safety.md, "Daily operator report"). Work through the admin links above; never '
        "open, download or visit a flagged page.",
        "",
        "This is the one consolidated operator mail and it is sent once a day even when every "
        "section above is quiet - individual event mails (an incident-rule block, a new TDM/AI "
        "reservation, a broken adapter) are off by default (OPERATOR_MAIL_IMMEDIATE brings one "
        "back). If this mail stops arriving, the reporting pipeline itself is broken.",
    ]
    return "\n".join(lines)


def load_blocks(start: datetime, end: datetime) -> list[Block]:
    """Pages that became blocked inside the window.

    `safety_flagged_at` is stamped when a page's safety status changes, so this is exactly the
    population the per-event alert used to mail about. Three columns are read on purpose: a page's
    title, URL and extraction must not be able to reach a mail body from here.
    """
    rows = (
        Page.objects.filter(
            safety_status=Page.SafetyStatus.BLOCKED,
            safety_flagged_at__gte=start,
            safety_flagged_at__lt=end,
        )
        .order_by("pk")
        .values_list("pk", "site__domain", "safety_matches")
    )
    return [
        Block(page_id=page_id, site_domain=domain, rules=parse_rules(matches))
        for page_id, domain, matches in rows
    ]


def load_adapter_alarms(start: datetime, end: datetime) -> list[AdapterAlarm]:
    """Adapters newly reported broken (or overdue for a repeat) inside the window.

    `crawler.adapter_alerts.notify_broken` stamps `AdapterHealth.notified_at` whenever a report is
    due, whether or not `OPERATOR_MAIL_IMMEDIATE` also mails it on its own — so this window read
    finds exactly the reports the individual mail would have carried, no more and no less.
    """
    rows = (
        AdapterHealth.objects.filter(
            status=AdapterHealth.Status.BROKEN,
            notified_at__gte=start,
            notified_at__lt=end,
        )
        .order_by("site__domain")
        .values_list("site__domain", "detail")
    )
    return [AdapterAlarm(domain=domain, detail=detail) for domain, detail in rows]


def load_tdm_reservations(
    start: datetime, end: datetime, *, now: datetime | None = None
) -> list[TdmReservation]:
    """Robots.txt reservations newly recorded or changed inside the window.

    `catalog.robots_alerts.notify` stamps `SiteRobotsPolicy.notified_at` the same way regardless of
    whether it also mails on its own, so this is exactly the population the individual mail used to
    cover. `paused` reads the site's *current* pause state (`catalog.robots_policy`), since a
    reservation pause is meant to stand until an operator reviews it, not just at detection time.
    """
    moment = now or timezone.now()
    rows = list(
        SiteRobotsPolicy.objects.filter(notified_at__gte=start, notified_at__lt=end)
        .select_related("site")
        .order_by("site__domain", "host")
    )
    return [
        TdmReservation(
            site_id=row.site_id,
            host=row.host,
            names_us=row.names_us,
            names_wildcard=row.names_wildcard,
            third_party_ai=row.third_party_ai,
            paused=robots_policy.paused_for_reservation(row.site, moment),
        )
        for row in rows
    ]


def _decisions(start: datetime, end: datetime) -> Counter[str]:
    kinds = SafetyRuleDecision.objects.filter(
        created_at__gte=start, created_at__lt=end
    ).values_list("kind", flat=True)
    return Counter(kinds)


def collect(start: datetime, end: datetime, *, now: datetime | None = None) -> Digest:
    """Read one window (and the one before it, for the comparison) out of the database."""
    decisions = _decisions(start, end)
    blocks = load_blocks(start, end)
    return Digest(
        start=start,
        end=end,
        current=tally(blocks),
        previous=tally(load_blocks(start - (end - start), start)),
        incidents_open=SafetyIncident.objects.filter(status=SafetyIncident.Status.OPEN).count(),
        incidents_reported=SafetyIncident.objects.filter(
            status=SafetyIncident.Status.REPORTED
        ).count(),
        incidents_opened=SafetyIncident.objects.filter(
            created_at__gte=start, created_at__lt=end
        ).count(),
        release_queue_waiting=Page.objects.filter(safety_release=Page.SafetyRelease.QUEUED).count(),
        released=decisions[SafetyRuleDecision.Kind.RELEASED],
        queued=decisions[SafetyRuleDecision.Kind.QUEUED],
        staff_changes=decisions[SafetyRuleDecision.Kind.STAFF_STATUS],
        incident_blocks=tuple(block for block in blocks if block.incident_class),
        adapter_alarms=tuple(load_adapter_alarms(start, end)),
        tdm_reservations=tuple(load_tdm_reservations(start, end, now=now)),
        data_requests=load_data_requests(),
    )


def window(now: datetime, last_succeeded_at: datetime | None) -> tuple[datetime, datetime]:
    """The period one report covers: since the last successful run, else the last 24 h.

    A scheduler that was down for two days therefore sends one mail covering both days rather than
    losing a day's events or repeating yesterday's.
    """
    start = last_succeeded_at if last_succeeded_at is not None else now - DEFAULT_WINDOW
    return (min(start, now), now)


def send(digest: Digest, *, recipients: Sequence[str] | None = None) -> None:
    # `using="default"` targets Django 6.1's MAILERS["default"] (marceline/settings.py), like
    # crawler.alerts: the classic EMAIL_BACKEND settings are deprecated and must not be mixed in.
    # No `fail_silently`: a failed send must raise, so the scheduler records the job FAILED and
    # never advances the window (marceline/scheduler.py `run_job`).
    EmailMessage(
        subject=subject(digest),
        body=body(digest),
        from_email=settings.SAFETY_ALERT_FROM,
        to=list(recipients if recipients is not None else alerts.recipients()),
    ).send(using="default")


def send_digest(start: datetime, end: datetime, *, dry_run: bool = False) -> str:
    """Build the one daily operator report for one window and mail it. Returns the `JobRun` line.

    Unlike the old digest, **always sends** while mail is configured — a fully quiet day still gets
    one short mail, because its absence is now the only signal that the whole pipeline stopped
    (2026-09-19). Raises nothing on purpose *except* a real send failure: that has to propagate so
    the scheduler marks the run failed and the window is retried, not skipped.
    """
    digest = collect(start, end)
    summary = (
        f"{digest.current.pages} page(s) blocked in {_hours(start, end)} "
        f"({_signed(digest.delta)} vs the window before), "
        f"{digest.current.incident_pages} by an incident rule, "
        f"{digest.incidents_open} incident(s) open, "
        f"{digest.release_queue_waiting} page(s) in the release queue, "
        f"{len(digest.adapter_alarms)} adapter alarm(s), "
        f"{len(digest.tdm_reservations)} TDM reservation(s), "
        f"{'?' if digest.data_requests is None else digest.data_requests.waiting} "
        "data request(s) waiting"
    )
    if dry_run:
        return f"{summary}; not sent (dry run)"
    if not alerts.configured():
        return f"{summary}; not sent (SAFETY_ALERT_TO/SAFETY_ALERT_FROM unset)"
    send(digest)
    logger.info("daily operator report sent: %s", summary)
    return f"{summary}; mailed"
