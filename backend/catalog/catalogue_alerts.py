"""Operator alert when our share of a site's catalogue crosses the monitoring threshold (#107).

Shape follows `crawler/adapter_alerts.py`: environment-driven, off unless an address pair is set,
nothing hardcoded, and **never raises** — an evaluation pass must not fail because it tried to
report something. The addresses are the operator's own safety pair (`SAFETY_ALERT_TO` /
`SAFETY_ALERT_FROM`): one operator, one mailbox.

**One mail per crossing.** `SiteCatalogueSize.notified_at` is stamped when a site goes over and
cleared when it falls back under, so a site sitting just above the threshold does not mail every
day, and a site that crosses again later does.

**It alerts, it never pauses.** The threshold is a monitoring number, not a legal line
(`catalog/catalogue_size.py`, ADR 0049): what it buys is that the operator hears about it early.
Nothing about the crawl changes.

**Content rule.** A domain, two counts, a percentage and a sentence of our own — never a page id,
a URL, a title or any text read off the site.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from catalog.catalogue_size import alert_percent, over_threshold
from catalog.models import SiteCatalogueSize

logger = logging.getLogger(__name__)

FOOTER = (
    "\nThis is a monitoring threshold (CATALOGUE_SHARE_ALERT_PERCENT), not a legal line. "
    "Nothing was paused and no crawl changed. Re-read "
    "docs/legal/crawl-position.md § 4.3 and decide; `manage.py catalogue_size` shows every site's "
    "share and where each total came from."
)


def _setting(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def recipients() -> list[str]:
    raw = _setting("SAFETY_ALERT_TO")
    return [address.strip() for address in raw.split(",") if address.strip()]


def sender() -> str:
    return _setting("SAFETY_ALERT_FROM")


def _line(record: SiteCatalogueSize) -> str:
    indexed = record.share_indexed or 0.0
    active = record.share_active or 0.0
    return (
        f"- {record.site.domain}: ever indexed {record.pages_indexed:,} of {record.total:,} "
        f"({indexed:.1f} %); active {record.pages_active:,} ({active:.1f} %); total from "
        f"{record.method}, seen {record.observed_at:%Y-%m-%d}"
    )


def _body(records: list[SiteCatalogueSize]) -> str:
    lines = [
        f"{len(records)} site(s) are at or above {alert_percent():.1f} % of their own catalogue:",
        "",
    ]
    lines.extend(_line(record) for record in records)
    lines.append(FOOTER)
    return "\n".join(lines)


def notify_over_threshold(records: list[SiteCatalogueSize]) -> int:
    """Mail and log the sites that newly crossed the threshold. Returns how many.

    `records` is every site with a known total; this picks the ones that are over and have not
    been reported since they last went over, and clears the stamp on the ones that are under.
    """
    try:
        over = [record for record in records if over_threshold(record)]
        under = [record.pk for record in records if not over_threshold(record)]
        if under:
            SiteCatalogueSize.objects.filter(pk__in=under, notified_at__isnull=False).update(
                notified_at=None
            )
        due = [record for record in over if record.notified_at is None]
        if not due:
            return 0
        for record in due:
            logger.error(
                "our index holds %.1f %% of %s's catalogue (%s of %s pages ever indexed)",
                record.share_indexed or 0.0,
                record.site.domain,
                record.pages_indexed,
                record.total,
            )
        addresses = recipients()
        if addresses and sender():
            EmailMessage(
                subject=f"Marceline: {len(due)} site(s) over the catalogue-share threshold",
                body=_body(due),
                from_email=sender(),
                to=addresses,
            ).send(using="default")
        SiteCatalogueSize.objects.filter(pk__in=[record.pk for record in due]).update(
            notified_at=timezone.now()
        )
        return len(due)
    except Exception:
        logger.exception("catalogue share notification failed")
        return 0
