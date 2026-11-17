"""Operator alert when a site's adapter looks broken (issue #22, third acceptance criterion).

A break is silent by nature — nothing crashes, the crawl keeps spending its budget, and the pages
it stores are empty. So the verdict has to reach the operator rather than wait in a table:

* the row is stamped `notified_at` the moment a site is newly reported broken (or overdue for a
  repeat, `REPEAT_HOURS`), so the daily report (`crawler/safety_digest.py`) can find what is new in
  its window regardless of whether anything below actually mails;
* one `logger.error`, which is what Sentry picks up (`core/sentry.py`), on the same transitions;
* an `EmailMessage`, only while `OPERATOR_MAIL_IMMEDIATE` names `adapter_health`
  (`core.operator_mail.is_immediate`) — off by default since 2026-09-19, when this folded into the
  daily report instead of mailing on its own.

Shape follows `crawler/alerts.py` (issue #40) and `crawler/backup_alerts.py` (issue #39):
environment-driven, off unless an address pair is set, nothing hardcoded, and **never raises** —
an evaluation pass must not fail because it tried to report something. `ADAPTER_ALERT_TO` /
`ADAPTER_ALERT_FROM` fall back to `SAFETY_ALERT_TO` / `SAFETY_ALERT_FROM`: one operator, one
mailbox.

**Content rule.** A site domain, a status, counters and a sentence of our own. Never a page id,
a URL, a title or any extracted text — this says a *parser* stopped working, and it needs nothing
about the pages to say it.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from core import operator_mail
from crawler.adapter_health import SiteReport
from crawler.models import AdapterHealth

logger = logging.getLogger(__name__)

#: A site that stays broken is mailed about again this often, not on every evaluation.
REPEAT_HOURS = 24

FOOTER = (
    "\nThe adapter is the truth: fix the parser in backend/crawler/sites/ and re-record its "
    "fixtures (CLAUDE.md, 'Adding a site adapter'). `manage.py adapter_health` shows the numbers "
    "behind this. If CRAWLER_LLM_REPAIR_ENABLED is on, a capped LLM fallback is keeping some "
    "fields flowing meanwhile (ADR 0042) — it is a stopgap, not a fix."
)


def _setting(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def recipients() -> list[str]:
    raw = _setting("ADAPTER_ALERT_TO") or _setting("SAFETY_ALERT_TO")
    return [address.strip() for address in raw.split(",") if address.strip()]


def sender() -> str:
    return _setting("ADAPTER_ALERT_FROM") or _setting("SAFETY_ALERT_FROM")


def _body(reports: list[SiteReport]) -> str:
    lines = [f"{len(reports)} site adapter(s) look broken:", ""]
    for report in reports:
        lines.append(f"- {report.domain}: {report.detail}")
        lines.append(
            f"  no page {report.no_page}, no title {report.no_title}, no media {report.no_media} "
            f"of {report.recent.parses} detail parse(s); repaired by model {report.llm_repaired}"
        )
    lines.append(FOOTER)
    return "\n".join(lines)


def notify_broken(reports: list[SiteReport], *, now: None = None) -> int:
    """Mail and log the sites that are broken and not recently reported. Returns how many.

    Call it after an evaluation pass with every report it produced; this picks the broken ones
    that are due. Stamps `AdapterHealth.notified_at` so the next pass stays quiet.
    """
    moment = timezone.now()
    try:
        due = []
        for report in reports:
            if report.status != AdapterHealth.Status.BROKEN:
                continue
            record = AdapterHealth.objects.filter(site_id=report.site_id).first()
            last = record.notified_at if record is not None else None
            if last is None or moment - last >= timedelta(hours=REPEAT_HOURS):
                due.append(report)
        if not due:
            return 0
        for report in due:
            logger.error("adapter for %s looks broken: %s", report.domain, report.detail)
        if operator_mail.is_immediate(operator_mail.ADAPTER_HEALTH):
            addresses = recipients()
            if addresses and sender():
                EmailMessage(
                    subject=f"Marceline crawler: {len(due)} adapter(s) look broken",
                    body=_body(due),
                    from_email=sender(),
                    to=addresses,
                ).send(using="default")
        AdapterHealth.objects.filter(site_id__in=[r.site_id for r in due]).update(
            notified_at=moment
        )
        return len(due)
    except Exception:
        logger.exception("adapter health notification failed")
        return 0
