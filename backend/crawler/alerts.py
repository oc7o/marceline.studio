"""Active safety notification (issue #40): one short mail when an incident-rule block happens.

**Only the incident class ever qualifies for an immediate mail (2026-09-18).** The operator asked
for one consolidated mail a day instead of a stream of alerts (`crawler/safety_digest.py`).
Measured on the live database, the ten-minute folding below still produced tens of mails a
day — and that number grows with every site the crawl runtime serves. So an ordinary block is not mailed at all; it is counted in the next digest.
What may go out at once is a block whose own matched rule opens a safety incident
(`catalog.safety_rules.opens_incident`: the `auto_block_and_incident` terms, every one of them
category `minor` today, plus any unknown or legacy action, which counts as strictest). Such a
block is not something to learn about tomorrow morning. `auto_block` (non-consent wording) and
`customer_release_queue` blocks — the large majority — wait for the digest.

**Off by default even for the incident class (2026-09-19).** The digest turned out to still throw
an individual mail on top of itself for every incident-rule block, so it now lists those
blocks in the one daily report (`crawler/safety_digest.py`) instead of mailing them one by one.
`core.operator_mail.is_immediate(SAFETY_INCIDENT)` — driven by `OPERATOR_MAIL_IMMEDIATE`, empty by
default — is the escape hatch back to the old per-event mail, unchanged otherwise: same batching,
same content rule, same failure handling. The block itself, and the `SafetyIncident` /
`SafetyRuleDecision` rows it produces, are written by `crawler.services` whether or not anything
ever mails.

Operator decision (2026-09-16): a `block` must actively reach the operator by mail, not only sit
in the moderation queue. Configuration is entirely environment-driven — `SAFETY_ALERT_TO`,
`SAFETY_ALERT_FROM` and the usual `EMAIL_*` variables, wired into Django 6.1's `MAILERS` setting
(`marceline/settings.py`, `.env.example`; the classic single `EMAIL_BACKEND`/`EMAIL_HOST`/...
settings are deprecated in this Django version and cannot be mixed with `MAILERS`). Nothing is
hardcoded: with `SAFETY_ALERT_TO` or `SAFETY_ALERT_FROM` unset the feature is off and nothing is
sent — that is the default — and the dev default mailer is the console backend, which never opens
a network connection.

**Content rule (non-negotiable, docs/safety.md).** The mail carries only the page id, its site's
domain, the matched safety categories and the matched term text (from `catalog.SafetyTerm`, the
project's own short indicator list — never page content) and a link into the internal moderation
admin. It never carries the page URL, title, any extracted text, or anything about the media: this
is mail about suspected illegal material, and it must be useless to anyone who intercepts it.
`BlockedPage.from_page` is the only place allowed to read a `Page`, and it reads exactly those
fields — nothing here ever touches `page.url`, `page.title`, `page.tags` or `page.raw_extraction`.

**No flooding.** A crawl, or a `safety_scan` rescan after a term edit, can block many pages within
seconds. Newly-blocked incident-rule pages are queued in process memory and sent as one mail every
`SAFETY_ALERT_INTERVAL_MINUTES` (default 10): the first block after a quiet period goes out right
away, everything behind it in the same window is folded into the next mail. The queue is
per-process and not persisted — a crash or restart can lose a pending (not yet due) batch, but
never drops a block itself: the page stays `blocked` and visible in the admin and the moderation
queue regardless of whether the mail went out. `flush_pending()` is called once at the
end of `crawler.services.scan_pages` so a one-shot batch job's mail is never left stranded in
memory when the process exits.

**Never breaks the caller.** Both public functions catch every exception (a bad SMTP config, a
network timeout, a template bug) and log it; the page they were called about is already committed
as `blocked` by the time either runs.
"""

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.urls import reverse
from django.utils import timezone

from catalog import safety_rules
from core import operator_mail
from index.models import Page

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlockedPage:
    """The only fields a safety alert mail may carry. Never the URL, title or any page text."""

    id: int
    site_domain: str
    categories: str
    terms: str

    @staticmethod
    def from_page(page: Page) -> "BlockedPage":
        raw = page.safety_matches if isinstance(page.safety_matches, list) else []
        rows = [m for m in raw if isinstance(m, dict)]
        categories = ", ".join(sorted({str(m["category"]) for m in rows if m.get("category")}))
        terms = ", ".join(sorted({str(m["term"]) for m in rows if m.get("term")}))
        return BlockedPage(
            id=page.pk,
            site_domain=page.site.domain,
            categories=categories or "(none recorded)",
            terms=terms or "(none recorded)",
        )


@dataclass
class _Batch:
    pages: list[BlockedPage] = field(default_factory=list)
    last_sent_at: datetime | None = None


_lock = threading.Lock()
_batch = _Batch()


def reset_batch_state() -> None:
    """Test-only: clears the in-process queue and last-sent timestamp between tests."""
    global _batch
    with _lock:
        _batch = _Batch()


def configured() -> bool:
    """Both addresses set: the one switch for the immediate alert and the daily digest alike."""
    return bool(settings.SAFETY_ALERT_TO.strip()) and bool(settings.SAFETY_ALERT_FROM.strip())


def mails_immediately(page: Page) -> bool:
    """Whether this blocked page is the class that may not wait for tomorrow's digest.

    Exactly the pages whose own matched rule opens a safety incident: the CSAM-suspect terms
    (`auto_block_and_incident`), and any unknown or legacy action, which `catalog.safety_rules`
    treats as strictest. A `review` page is not blocked at all; `auto_block` and
    `customer_release_queue` blocks are counted in the digest instead.
    """
    return page.safety_status == Page.SafetyStatus.BLOCKED and safety_rules.opens_incident(
        page.safety_matches
    )


def _interval() -> timedelta:
    minutes = int(getattr(settings, "SAFETY_ALERT_INTERVAL_MINUTES", 10))
    return timedelta(minutes=max(minutes, 0))


def recipients() -> list[str]:
    return [addr.strip() for addr in settings.SAFETY_ALERT_TO.split(",") if addr.strip()]


def admin_url(viewname: str, *args: object) -> str:
    """An internal admin link, absolute when `SAFETY_ALERT_MODERATION_URL` names the origin.

    Shared with the daily digest (`crawler/safety_digest.py`) so both mails point at the same
    Tailscale-only admin, and neither ever builds a link to a site we crawl.
    """
    path = reverse(viewname, args=list(args))
    base = str(getattr(settings, "SAFETY_ALERT_MODERATION_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else path


def _moderation_link(page_id: int) -> str:
    return admin_url("admin:index_page_change", page_id)


def _body(pages: Sequence[BlockedPage]) -> str:
    lines = [
        f"{len(pages)} page(s) blocked by a rule that opens a safety incident, since the last "
        "alert. Every other block is counted in the daily digest, not mailed:",
        "",
    ]
    for page in pages:
        lines.append(
            f"- page {page.id} - site {page.site_domain} - category {page.categories} "
            f'- term "{page.terms}" - {_moderation_link(page.id)}'
        )
    lines += [
        "",
        "This mail carries no page URL, title or extracted text on purpose (docs/safety.md, "
        '"Active safety notification"). Do not open, download or visit the pages above; use the '
        "moderation admin link and follow the incident procedure in docs/safety.md.",
    ]
    return "\n".join(lines)


def _send(pages: Sequence[BlockedPage]) -> None:
    if not pages:
        return
    # `using="default"` targets Django 6.1's MAILERS["default"] (marceline/settings.py) and avoids
    # every RemovedInDjango70Warning code path of the classic send_mail()/get_connection() API.
    EmailMessage(
        subject=f"Marceline safety: {len(pages)} page(s) blocked by an incident rule",
        body=_body(pages),
        from_email=settings.SAFETY_ALERT_FROM,
        to=recipients(),
    ).send(using="default")


def _try_send(*, now: datetime | None = None, force: bool = False) -> None:
    now = now or timezone.now()
    with _lock:
        if not _batch.pages:
            return
        stale_enough = _batch.last_sent_at is None or now - _batch.last_sent_at >= _interval()
        if not force and not stale_enough:
            return
        pages, _batch.pages = _batch.pages, []
        _batch.last_sent_at = now
    _send(pages)


def notify_block(page: Page, *, now: datetime | None = None) -> None:
    """Queue one newly-blocked page and send it (with anything else queued) if the alert is due.

    Call once per page right after its `blocked` status is committed — never inside the storing
    transaction (use `transaction.on_commit`): a mail server may take seconds to answer, and the
    page's row lock must not wait for it (docs/safety.md). A no-op when
    `SAFETY_ALERT_TO`/`SAFETY_ALERT_FROM` are unset (the default), a no-op for a block no incident
    rule decided (`mails_immediately`) — that one is the digest's business — and, since 2026-09-19,
    a no-op unless `OPERATOR_MAIL_IMMEDIATE` names `safety_incident`
    (`core.operator_mail.is_immediate`): by default even an incident-rule block waits for the one
    daily report, which lists it (`crawler/safety_digest.py`). Never raises: a send failure is
    logged and swallowed so the crawl or scan that called this is never affected.
    """
    try:
        if not configured() or not mails_immediately(page):
            return
        if not operator_mail.is_immediate(operator_mail.SAFETY_INCIDENT):
            return
        with _lock:
            _batch.pages.append(BlockedPage.from_page(page))
        _try_send(now=now)
    except Exception:
        logger.exception("safety alert notification failed for page=%s", page.pk)


def flush_pending(*, now: datetime | None = None) -> None:
    """Force-send whatever is queued, ignoring the interval.

    Used once at the end of a one-shot batch job (`safety_scan`) so pages it blocked are not left
    waiting in memory for a mail that may never come if nothing crawls again soon. Never raises.
    """
    try:
        if not configured():
            return
        _try_send(now=now, force=True)
    except Exception:
        logger.exception("safety alert flush failed")
