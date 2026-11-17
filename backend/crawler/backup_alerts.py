"""Backup failure alert (issue #39): one mail when a backup run or the backup watchdog fails.

A backup that fails silently is worse than no backup, because it buys confidence nobody has
earned. The shell tooling in `infra/backup/` therefore calls this module (through the
`backup_alert` management command) in exactly two situations:

* a run failed — `infra/backup/backup-run.sh` could not dump, verify or push the database;
* a run has not happened — `infra/backup/backup-check.sh` found the newest local dump, the newest
  off-host snapshot or the last restore check older than its limit. A cron entry that quietly
  stops firing is the classic silent failure, and it produces no failing run to report.

Shape follows `crawler/alerts.py` (issue #40): configuration is entirely environment-driven,
the feature is off unless the operator sets both addresses, nothing is ever hardcoded, and
sending never raises — a backup script must not fail *because* it tried to report a failure.
Mail goes through Django 6.1's `MAILERS["default"]` (`marceline/settings.py`), so the `EMAIL_*`
variables that already carry the safety alert carry this one too, and the dev default stays the
console backend, which never opens a network connection.

`BACKUP_ALERT_TO` / `BACKUP_ALERT_FROM` fall back to `SAFETY_ALERT_TO` / `SAFETY_ALERT_FROM`: one
operator, one mailbox, and a backup alert is not more sensitive than a safety alert. They are read
from the environment here rather than added to `marceline/settings.py` so that the backup tooling
stays self-contained; `.env` is loaded by settings on import either way (`core.env.load_dotenv`).

**Content rule.** The body is the tail of a backup script's own output: timestamps, file paths,
sizes, row counts and tool errors. Those scripts print no page URL, page title or safety record,
and nothing here goes looking for one — this mail must stay useless to anyone who intercepts it,
exactly like the safety alert (docs/safety.md). The repository location is redacted by the caller
(`infra/backup/common.sh`), so a leaked mail does not also hand over the backup target.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMessage

from core.env import env_str

logger = logging.getLogger(__name__)

SUBJECT_PREFIX = "Marceline backup: "
MAX_SUBJECT_CHARS = 120
# Enough to carry a script's summary plus the tool error under it; the full output stays in the
# log file on the server, and a mail is a notification, not a log shipper.
MAX_BODY_CHARS = 4000

FOOTER = (
    "\nThis is an automated message from infra/backup/ (issue #39). The full output is in the "
    "backup log on the server; see docs/runbooks/backup-restore.md for what to do next."
)


def _setting(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def recipients() -> list[str]:
    raw = env_str("BACKUP_ALERT_TO", default="") or _setting("SAFETY_ALERT_TO")
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def sender() -> str:
    return (env_str("BACKUP_ALERT_FROM", default="") or _setting("SAFETY_ALERT_FROM")).strip()


def configured() -> bool:
    """True when both a recipient and a sender are configured. Off by default, like issue #40."""
    return bool(recipients()) and bool(sender())


def _trim_subject(subject: str) -> str:
    one_line = " ".join(subject.split())
    if len(one_line) > MAX_SUBJECT_CHARS:
        one_line = one_line[: MAX_SUBJECT_CHARS - 1].rstrip() + "…"
    return f"{SUBJECT_PREFIX}{one_line}"


def _trim_body(body: str) -> str:
    text = body.strip()
    if len(text) > MAX_BODY_CHARS:
        # Keep the tail: the error that ended the run is at the bottom of the output.
        text = "…(earlier output omitted)…\n" + text[-MAX_BODY_CHARS:]
    return f"{text}\n{FOOTER}"


def send_backup_alert(subject: str, body: str) -> bool:
    """Mail one backup failure. Returns True when a mail was handed to the mailer.

    Returns False when the alert is not configured (the default) or the send failed; both are
    logged. Never raises: the caller is a backup script that has already failed, and it still has
    to report that failure on stdout and to its exit code.
    """
    try:
        if not configured():
            logger.warning(
                "backup alert not sent (BACKUP_ALERT_TO/FROM and SAFETY_ALERT_TO/FROM unset): %s",
                subject,
            )
            return False
        # `using="default"` targets MAILERS["default"] (marceline/settings.py), like crawler.alerts.
        EmailMessage(
            subject=_trim_subject(subject),
            body=_trim_body(body),
            from_email=sender(),
            to=recipients(),
        ).send(using="default")
        return True
    except Exception:
        logger.exception("backup alert could not be sent: %s", subject)
        return False
