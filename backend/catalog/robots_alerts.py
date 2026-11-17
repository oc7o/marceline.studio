"""Operator alert when a site's robots.txt reservation appears or changes (issue #105).

Shape follows `crawler/adapter_alerts.py` (issue #22): environment-driven, off unless an address
pair is set, one `logger.error` (which is what Sentry picks up) on the same event, and it **never
raises** — a crawl must not fail because it tried to report something. `ADAPTER_ALERT_TO` /
`ADAPTER_ALERT_FROM` fall back to `SAFETY_ALERT_TO` / `SAFETY_ALERT_FROM`: one operator, one
mailbox.

Once per **new or changed** reservation per site and host: `SiteRobotsPolicy.notified_hash` holds
the content hash the last mail was about, so a re-read of the same file is silent for ever and an
extended list of tokens is a new mail. **That stamp is written regardless of whether an
`EmailMessage` actually goes out** — since 2026-09-19 the row still mails on its own only while
`OPERATOR_MAIL_IMMEDIATE` names `tdm_reservation` (`core.operator_mail.is_immediate`); by default
this folds into the one daily report instead (`crawler/safety_digest.py`, which reads
`notified_at` to find what is new in its window), and the `logger.error` still fires either way.

**Content rule.** A Site id, a host, the agent tokens the site itself names, and a sentence of our
own. Never a page URL, a title, any page text, or a line of the robots.txt.
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import EmailMessage

from catalog.models import SiteRobotsPolicy
from core import operator_mail

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from catalog.robots_policy import Outcome

logger = logging.getLogger(__name__)

FOOTER = (
    "\nWhat to do: read docs/legal/crawl-position.md § 3.1 and § 4.2 (tripwire T2). A reservation "
    "naming our own user agent, or a TDM-Reservation / X-Robots-Tag: noai header, is unambiguous — "
    "the site stays paused until you decide. A reservation naming only other companies' AI "
    "crawlers is recorded, not acted on: decide per site. "
    "`manage.py request_report --site <domain>` and the Site admin page show what is stored."
)


def _setting(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def recipients() -> list[str]:
    raw = _setting("ADAPTER_ALERT_TO") or _setting("SAFETY_ALERT_TO")
    return [address.strip() for address in raw.split(",") if address.strip()]


def sender() -> str:
    return _setting("ADAPTER_ALERT_FROM") or _setting("SAFETY_ALERT_FROM")


def _body(site_id: int, row: SiteRobotsPolicy, outcome: "Outcome") -> str:
    lines = [
        f"Site {site_id}, host {row.host}: its robots.txt declares a machine-readable reservation.",
        "",
        f"- names our own user agent: {'yes' if row.names_us else 'no'}",
        f"- names `*` (already obeyed, every URL skipped): {'yes' if row.names_wildcard else 'no'}",
        f"- other AI/TDM tokens fully disallowed: {', '.join(row.ai_tokens) or 'none'}",
        f"- reservation header(s) on the robots.txt response: "
        f"{', '.join(row.reservation_headers) or 'none'}",
        f"- groups in the file: {len(row.agents)}; fully disallowed: "
        f"{len(row.full_disallow_agents)}",
        f"- first seen {row.first_seen_at:%Y-%m-%d %H:%M} UTC, last changed "
        f"{row.changed_at:%Y-%m-%d %H:%M} UTC",
        "",
        (
            "The site is PAUSED pending your review."
            if outcome.paused
            else (
                "The site is NOT paused: ROBOTS_RESERVATION_PAUSE_ENABLED is off."
                if outcome.reserved_against_us
                else "Nothing was paused: the reservation does not address us."
            )
        ),
        FOOTER,
    ]
    return "\n".join(lines)


def notify(site_id: int, outcome: "Outcome", *, now: datetime) -> bool:
    """Mail and log this reservation once. Returns whether an alert was sent or logged."""
    try:
        row = SiteRobotsPolicy.objects.filter(site_id=site_id, host=outcome.host).first()
        if row is None or row.notified_hash == row.content_hash:
            return False
        logger.error(
            "site %s host %s declares a robots.txt reservation (names us: %s, wildcard: %s, "
            "third-party AI tokens: %d, headers: %d); paused: %s",
            site_id,
            row.host,
            row.names_us,
            row.names_wildcard,
            len(row.ai_tokens),
            len(row.reservation_headers),
            outcome.paused,
        )
        if operator_mail.is_immediate(operator_mail.TDM_RESERVATION):
            addresses = recipients()
            if addresses and sender():
                EmailMessage(
                    subject=f"Marceline crawler: site {site_id} declares a TDM/AI reservation",
                    body=_body(site_id, row, outcome),
                    from_email=sender(),
                    to=addresses,
                ).send(using="default")
        SiteRobotsPolicy.objects.filter(pk=row.pk).update(
            notified_at=now, notified_hash=row.content_hash
        )
        return True
    except Exception:
        logger.exception("robots reservation notification failed")
        return False
