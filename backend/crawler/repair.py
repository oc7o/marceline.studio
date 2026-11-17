"""LLM extraction as repair when an adapter breaks (issue #22, ADR 0042).

Half two of the issue. `crawler.adapter_health` notices that a site's parser has stopped
extracting; this decides whether a model may be asked to read the fields off the page text
instead, and merges what comes back.

**It costs money per call, so it is off, capped and never in a loop.** Four conditions, all of
them, before a single request is made:

1. `CRAWLER_LLM_REPAIR_ENABLED` is on. Default off; the operator turns it on knowingly.
2. `crawler.AdapterHealth` records *this site* as broken. That is a stored verdict from a separate
   evaluation pass, not something this code can decide for itself in the hot path.
3. The parse in hand actually came back blind — the caller only gets here for those.
4. This site has spent fewer than `CRAWLER_LLM_REPAIR_DAILY_CALLS_PER_SITE` calls in the last
   24 h, counted from the `LLMCall` rows themselves, so a crash, a restart or a second worker
   cannot reset it.

**One call per page, ever.** There is no retry: a refusal, a network error or an answer that does
not validate ends it, the adapter's own result is stored unchanged, and the next attempt can only
come from a later crawl of that URL. Nothing here re-queues, re-asks or escalates.

**Nothing leaves before the safety terms have judged it** (ADR 0042 amendment 2026-09-18). The
text this would send is the one thing on this path that nothing has looked at: a blind parse means
the adapter found no title and no media, so `store_parse_result`'s terms, score and media gate have
had nothing to judge. `crawler.text_gate.transmission_refusal` therefore judges the excerpt itself,
before any request is built — `crawler.llm.extract_page_fields` calls it as its first statement —
and a refusal is recorded by rule name, never by text, and costs no budget.

**What it may change.** Only fields the adapter left empty — the model never overwrites something
the parser found. The merged text then goes through `store_parse_result` exactly like adapter text
does: the same safety terms, the same score, the same media gate. Media URLs and discovered links
are **never** taken from the model (`crawler/llm.py`, containment layer 2), so a repaired page
carries the adapter's media or none at all. That is the deliberate limit of this feature: it keeps
titles, durations and credits flowing — which is what creator linking, breadcrumbs and metadata
scoring (ADR 0040) need — while the parser is fixed by hand. Restoring media is the adapter's job.
"""

import logging
from datetime import timedelta

import httpx
from django.conf import settings
from django.utils import timezone

from crawler import text_gate
from crawler.llm import (
    LLMUnavailable,
    PageFields,
    clean_page_text,
    configured,
    extract_page_fields,
)
from crawler.models import AdapterHealth, CrawlTask, LLMCall
from crawler.sites.base import PageData, ParseResult

logger = logging.getLogger(__name__)

#: `index.Page.extraction_source` value a repaired page is stored with.
LLM_REPAIR = "llm_repair"
#: Key under `PageData.extra` (so `Page.raw_extraction`) naming what the model supplied.
PROVENANCE_KEY = "llm_repair"


def enabled() -> bool:
    return bool(settings.CRAWLER_LLM_REPAIR_ENABLED) and configured()


def site_is_broken(site_id: int) -> bool:
    return AdapterHealth.objects.filter(
        site_id=site_id, status=AdapterHealth.Status.BROKEN
    ).exists()


def calls_today(site_id: int) -> int:
    """Repair calls already made for this site in the last 24 h, from the audit log itself.

    A row the safety gate refused (`crawler.llm.record_refusal`) is *not* a call: nothing was
    built and nothing was sent, so it costs nothing and may not spend anything either. Excluding
    it also keeps the refusal harmless — a site whose pages keep being refused must not thereby
    lose the repairs it is entitled to.
    """
    since = timezone.now() - timedelta(hours=24)
    return (
        LLMCall.objects.filter(
            site_id=site_id, purpose=LLMCall.Purpose.ADAPTER_REPAIR, created_at__gte=since
        )
        .exclude(error__startswith=text_gate.REFUSED)
        .count()
    )


def budget_left(site_id: int) -> int:
    cap = int(settings.CRAWLER_LLM_REPAIR_DAILY_CALLS_PER_SITE)
    return max(cap - calls_today(site_id), 0)


def repair_parse(
    task: CrawlTask,
    result: ParseResult,
    html: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ParseResult:
    """Fill in what the adapter left empty, or hand `result` straight back.

    Returning the *same object* means "nothing was repaired"; the caller compares by identity.
    `transport` is the test seam and nothing else passes it: the suite never opens a socket.
    """
    if not enabled() or not site_is_broken(task.site_id) or budget_left(task.site_id) <= 0:
        return result
    # Exactly what would be sent: cleaned, truncated, and judged in that form by the gate inside
    # `extract_page_fields`. Never judge the HTML instead — what leaves is this string.
    page_text = clean_page_text(html)
    if not page_text.strip():
        return result
    try:
        fields = extract_page_fields(
            page_text, task=task, site_id=task.site_id, transport=transport
        )
    except LLMUnavailable as exc:
        logger.warning("adapter repair is enabled but unusable: %s", exc)
        return result
    if fields is None:
        return result
    return merge(result, fields, task_url=task.url)


def merge(result: ParseResult, fields: PageFields, *, task_url: str) -> ParseResult:
    """Pure: the adapter's result plus the model's answer, the adapter always winning.

    Every branch below is "the adapter said nothing here, so use the model's value" — never the
    other way round, so a repair can only ever add to what the parser managed to find.
    """
    title = fields.title or ""
    duration_s = fields.duration_s
    performers = tuple(fields.performers)
    tags = tuple(fields.tags)
    page = result.page
    supplied: list[str] = []
    if page is None:
        if not title:
            # Without a title there is no page worth storing: a model that found nothing has
            # found nothing, and an untitled placeholder row would only hide the break.
            return result
        supplied = [name for name, value in (("title", title), ("duration_s", duration_s)) if value]
        supplied += [name for name, value in (("performers", performers), ("tags", tags)) if value]
        page = PageData(url=task_url, title=title)
    else:
        if page.title.strip():
            title = page.title
        elif title:
            supplied.append("title")
        if page.duration_s is not None:
            duration_s = page.duration_s
        elif duration_s is not None:
            supplied.append("duration_s")
        if page.performers:
            performers = page.performers
        elif performers:
            supplied.append("performers")
        if page.tags:
            tags = page.tags
        elif tags:
            supplied.append("tags")
    if not supplied:
        return result
    repaired = PageData(
        url=page.url,
        title=title,
        tags=tags,
        performers=performers,
        uploaded_at=page.uploaded_at,
        duration_s=duration_s,
        # The provenance travels with the data. `PageData.extracted_by` is what becomes the
        # queryable column; this is the detail a cleanup would want beside it.
        extra={**dict(page.extra), PROVENANCE_KEY: {"fields": supplied}},
        extracted_by=LLM_REPAIR,
    )
    return ParseResult(
        removed=result.removed, page=repaired, links=result.links, media=result.media
    )
