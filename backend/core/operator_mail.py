"""Which operator-mail *kinds* still mail immediately, one event at a time (2026-09-19).

The operator was getting a daily safety digest (good) plus three individual mails on top of it —
one per incident-rule block, one per new/changed robots.txt TDM/AI reservation, one per adapter
that looks broken — because each of those events used to mail the moment it happened
(`crawler/alerts.py`, `catalog/robots_alerts.py`, `crawler/adapter_alerts.py`). All three now fold
into the one daily report (`crawler/safety_digest.py`) by default: the event is still recorded
exactly as before (a blocked page, a `SiteRobotsPolicy` row, an `AdapterHealth` row) and still
logged once (`logger.error`, which is what Sentry picks up), but the per-event `EmailMessage` is
sent only for a kind named here.

`OPERATOR_MAIL_IMMEDIATE` is a comma list of kinds, default **empty** — so by default none of them
mail on their own — read fresh on every call so a `settings` override in a test, or an operator
edit to `.env` followed by a container recreate, takes effect without a code change. Backup mails
(`crawler/backup_alerts.py`) are a different system (shell scripts, not Django) and are not a kind
here on purpose — they stay immediate, as decided (2026-09-19).

Pure: reads `settings.OPERATOR_MAIL_IMMEDIATE` only, no I/O, no database.
"""

from django.conf import settings

#: A block whose own matched rule opens a safety incident (`crawler/alerts.py`).
SAFETY_INCIDENT = "safety_incident"
#: A site's robots.txt declares a new or changed machine-readable TDM/AI reservation
#: (`catalog/robots_alerts.py`).
TDM_RESERVATION = "tdm_reservation"
#: A site's adapter is newly recorded broken (`crawler/adapter_alerts.py`).
ADAPTER_HEALTH = "adapter_health"

#: Every kind this switch knows about — the only valid entries in `OPERATOR_MAIL_IMMEDIATE`.
KINDS = frozenset({SAFETY_INCIDENT, TDM_RESERVATION, ADAPTER_HEALTH})


def immediate_kinds() -> frozenset[str]:
    """The kinds `OPERATOR_MAIL_IMMEDIATE` currently names, whatever else it also names."""
    raw = str(getattr(settings, "OPERATOR_MAIL_IMMEDIATE", "") or "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def is_immediate(kind: str) -> bool:
    """Should this kind still mail the moment its event happens, rather than wait for the report?"""
    return kind in immediate_kinds()
