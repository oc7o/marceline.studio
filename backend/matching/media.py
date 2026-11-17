"""Media fetch queue: claim, finish and retry `index.Media` rows and store their PDQ hashes.

Every database workflow of the media worker lives here; fetching and hashing do not.

Scan once (ADR 0015): media served by a known mirror CDN under the key of an already hashed
media is finished with copies of its frame hashes before any download (`find_url_source`), and
downloaded bytes whose sha256 is already hashed are not decoded again (`find_sha256_source`).
Sources and targets are always on safety-clear pages; `MediaReuse` records each copy.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Case, Exists, F, IntegerField, OuterRef, Q, QuerySet, Value, When
from django.utils import timezone

from core.http import host_key
from index.models import FrameHash, Media, Page
from index.services import (
    SAFETY_BLOCK_REASON,
    SAFETY_REVIEW_REASON,
    media_denial_reason,
    page_media_allowed,
)
from matching.models import MediaReuse
from matching.pdq import HashedFrame
from matching.services import media_content_key

# Media URLs that stop working soon after the crawl. site03's CDN paths carry a per-asset token
# and a 10-digit expiry timestamp: `/tkn/{x}/{y}/vid/{token}/{ts}/{uploader}/{post}/full.jpg`
# (post posters, card thumbnails, preview clips) and `/tkn/{x}/{y}/img/{token}/{ts}/{a}/{b}/0.webp`
# (listing card posters, see crawler/tests/fixtures/site03.example/listing.html). The token is issued
# about 40 minutes before `ts` and the CDN answers 404 shortly after it. No other adapter's
# recorded media carries an expiry (checked 2026-09-17 against every fixture).
EXPIRING_URL_REGEX = r"/(?:vid|img)/[^/]+/[0-9]{10}/"

MAX_ATTEMPTS = 5
RETRY_BASE_DELAY = timedelta(minutes=1)
RETRY_MAX_DELAY = timedelta(hours=6)
STALE_LOCK_AFTER = timedelta(minutes=15)
ERROR_MAX_LENGTH = 5000

_FINISH_FIELDS = (
    "status",
    "locked_at",
    "next_attempt_at",
    "fetched_at",
    "last_error",
    "content_type",
    "byte_size",
)


def claimable_media() -> QuerySet[Media]:
    """Pending media the worker may still be handed, whatever the clock says.

    One definition, because two places need the same answer: `claim_media` (plus the schedule and
    the ordering) and `defer_media` (which only holds back rows a claim could otherwise pick up).
    """
    return (
        Media.objects.filter(status=Media.Status.PENDING)
        # Flagged pages (docs/safety.md) never have their media fetched, and media is only
        # eligible once its own detail page was parsed and scored clearly safe (ADR 0018).
        .filter(page__safety_status=Page.SafetyStatus.CLEAR)
        .filter(eligible_at__isnull=False)
        # Inactive pages (issue #71) keep their queued media for a later reactivation, but no
        # request is spent on them meanwhile. This is ordering of work, not the safety gate.
        .filter(page__active=True)
        .filter(page__site__enabled=True)
        # Browser-capture rows (ADR 0016) hold hashes, not a fetchable URL. They are already
        # ineligible; excluding the kind says so even if someone ever sets `eligible_at`.
        .exclude(kind=Media.Kind.CAPTURE)
    )


def claim_media(*, site_domain: str | None = None, now: datetime | None = None) -> Media | None:
    """Lock the most urgent available media row and mark it RUNNING. Safe across worker processes.

    Media with expiring URLs go first, then media of pages linked to any tracked `catalog.Creator`
    (any performer of a page counts, not only a primary credit) — so a page's evidence is already
    hashed by the time an original is imported for it, instead of waiting behind the rest of the
    queue (issue #9). Within that, a page's poster/primary frame — or the photo that *is* the page
    (issue #69) — is claimed before its hover frames, so every creator page contributes some
    evidence before any page is finished many frames deep. Ties break newest first. Eligibility
    itself (ADR 0018) is unaffected by this ordering; this only decides the order among rows
    already eligible.
    """
    now = now or timezone.now()
    with transaction.atomic():
        candidates = (
            claimable_media()
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .alias(
                creator_linked=Exists(
                    Page.creators.through.objects.filter(page_id=OuterRef("page_id"))
                ),
                expiring=Case(
                    When(url__regex=EXPIRING_URL_REGEX, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                ),
                hover_frame=Case(
                    When(kind=Media.Kind.FRAME, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                ),
            )
            .order_by("-expiring", "-creator_linked", "hover_frame", "-created_at", "-id")
        )
        if site_domain:
            # Filter through the site join, not a literal site id: with a literal id the planner
            # trusted the LIMIT on sites with nothing pending and sorted every pending row of
            # every site (review PERF, measured on a copy of the live tables).
            candidates = candidates.filter(page__site__domain=site_domain)
        # Only the id is sorted and locked (review PERF: wide rows made every claim sort ~1 kB
        # tuples); the row itself is read once, after it is ours.
        media_id = (
            candidates.select_for_update(skip_locked=True, of=("self",))
            .values_list("pk", flat=True)
            .first()
        )
        if media_id is None:
            return None
        Media.objects.filter(pk=media_id).update(
            status=Media.Status.RUNNING, locked_at=now, attempts=F("attempts") + 1
        )
        return Media.objects.select_related("page__site").get(pk=media_id)


def store_hashes(
    media: Media,
    frames: Sequence[HashedFrame],
    *,
    sha256: str,
    width: int | None = None,
    height: int | None = None,
    embedding_pipeline: str = "",
    now: datetime | None = None,
) -> None:
    """Replace the media's frame hashes and mark it DONE. Re-hashing is idempotent.

    `embedding_pipeline` is the recipe that produced the frames' sketches (ADR 0037); it is
    stored per frame, so a later model change is a new tag beside the old rows rather than a
    reinterpretation of them. Frames without a sketch store neither.
    """
    now = now or timezone.now()
    with transaction.atomic():
        gate = _page_gate(media.page_id)
        if not gate.allowed:
            # The page was flagged or gated while this media was being fetched: keep no hashes.
            FrameHash.objects.filter(media=media).delete()
            skip_media(media, gate.reason, now=now)
            return
        MediaReuse.objects.filter(media=media).delete()
        FrameHash.objects.filter(media=media).delete()
        FrameHash.objects.bulk_create(
            FrameHash(
                media=media,
                frame_index=f.frame_index,
                pdq=f.hash,
                quality=f.quality,
                embedding=f.embedding,
                embedding_pipeline=embedding_pipeline if f.embedding else "",
            )
            for f in frames
        )
        media.status = Media.Status.DONE
        media.sha256 = sha256
        media.width = width
        media.height = height
        media.fetched_at = now
        media.locked_at = None
        media.next_attempt_at = None
        media.last_error = ""
        media.save(update_fields=(*_FINISH_FIELDS, "sha256", "width", "height"))


# Kinds hashed by the same decoder give identical frames for identical bytes.
_HASH_FAMILY: dict[str, str] = {
    Media.Kind.POSTER: "image",
    Media.Kind.FRAME: "image",
    Media.Kind.SPRITE: "sprite",
    Media.Kind.PREVIEW: "video",
    # A photo post's image is the work itself (issue #69), but the same decoder reads it, so the
    # same bytes still give the same frames: it may reuse hashes from a poster and vice versa.
    Media.Kind.PHOTO: "image",
    # Browser-captured frames (ADR 0016) have no bytes and no fetchable URL, so they are neither
    # a reuse source nor a reuse target. Their own family keeps them out of both sides.
    Media.Kind.CAPTURE: "capture",
}
# Candidate sources checked per URL key (the key prefilter is a substring match).
_URL_SOURCE_LIMIT = 20


def reuse_sources(media: Media) -> QuerySet[Media]:
    """Media whose frame hashes may be copied to `media`: DONE, with hashes, same decoder, on a
    safety-clear page. Oldest first."""
    family = _HASH_FAMILY.get(media.kind)
    kinds = [kind for kind, value in _HASH_FAMILY.items() if value == family]
    return (
        Media.objects.filter(
            status=Media.Status.DONE,
            kind__in=kinds,
            page__safety_status=Page.SafetyStatus.CLEAR,
        )
        .exclude(pk=media.pk)
        .filter(Exists(FrameHash.objects.filter(media_id=OuterRef("pk"))))
        .order_by("pk")
    )


def find_url_source(media: Media) -> tuple[Media, str] | None:
    """A hashed media served under the same known-mirror URL key, found before any download."""
    key = media_content_key(media.url)
    if key is None:
        return None
    for source in reuse_sources(media).filter(url__contains=key.needle)[:_URL_SOURCE_LIMIT]:
        if media_content_key(source.url) == key:
            return source, key.value
    return None


def find_sha256_source(media: Media, sha256: str) -> Media | None:
    """A hashed media with exactly these bytes."""
    if not sha256:
        return None
    return reuse_sources(media).filter(sha256=sha256).first()


def reuse_hashes(
    media: Media,
    source: Media,
    *,
    reason: MediaReuse.Reason,
    key: str,
    sha256: str = "",
    width: int | None = None,
    height: int | None = None,
    fetched: bool = False,
    now: datetime | None = None,
) -> bool:
    """Finish `media` with copies of `source`'s frame hashes instead of hashing it.

    Returns False and writes nothing when the source is no longer usable (lost its hashes,
    re-queued, or its page was flagged); the caller then hashes normally. Media of a page flagged
    meanwhile is skipped like in `store_hashes` (returns True). URL-key reuse leaves sha256 and
    size empty: nothing was downloaded.
    """
    now = now or timezone.now()
    with transaction.atomic():
        gate = _page_gate(media.page_id)
        if not gate.allowed:
            FrameHash.objects.filter(media=media).delete()
            skip_media(media, gate.reason, fetched=fetched, now=now)
            return True
        if not reuse_sources(media).filter(pk=source.pk).exists():
            return False
        # The sketch travels with the hashes: identical bytes through the same decoder give the
        # same picture, so the same sketch (ADR 0037). A source hashed before embeddings were on
        # simply carries none, and that means "no information", never "no match".
        frames = list(
            FrameHash.objects.filter(media_id=source.pk)
            .order_by("frame_index")
            .values_list("frame_index", "pdq", "quality", "embedding", "embedding_pipeline")
        )
        FrameHash.objects.filter(media=media).delete()
        FrameHash.objects.bulk_create(
            FrameHash(
                media=media,
                frame_index=index,
                pdq=bytes(pdq),
                quality=quality,
                embedding=None if sketch is None else bytes(sketch),
                embedding_pipeline=pipeline,
            )
            for index, pdq, quality, sketch, pipeline in frames
        )
        media.status = Media.Status.DONE
        media.sha256 = sha256
        media.width = width
        media.height = height
        if fetched:
            media.fetched_at = now
        media.locked_at = None
        media.next_attempt_at = None
        media.last_error = ""
        media.save(update_fields=(*_FINISH_FIELDS, "sha256", "width", "height"))
        MediaReuse.objects.update_or_create(
            media=media,
            defaults={"source": source, "reason": reason, "key": key[:2000], "frames": len(frames)},
        )
        return True


@dataclass(frozen=True)
class _PageGate:
    """Whether a page may have hashes at all, and why not."""

    allowed: bool
    reason: str = ""


def _page_gate(page_id: int) -> _PageGate:
    """Re-read the page's safety status and score and decide whether hashes may be kept.

    Call inside the transaction that writes the hashes. The page row stays locked until that
    transaction ends (`FOR NO KEY UPDATE`: every safety write to the page waits, inserting media
    for it does not), so a block cannot commit between this check and the write — it waits, and
    its `cancel_page_media` then removes what was written. Without the lock a block landing in
    that window left frame hashes and a DONE media row on a blocked page.
    """
    row = (
        Page.objects.select_for_update(no_key=True)
        .filter(pk=page_id)
        .values_list("safety_status", "safety_score")
        .first()
    )
    if row is None:
        return _PageGate(False, SAFETY_BLOCK_REASON)
    status, score = row
    if page_media_allowed(status, score):
        return _PageGate(True)
    return _PageGate(False, media_denial_reason(status, score))


def safety_skip_reason(page_status: str | None) -> str:
    return SAFETY_REVIEW_REASON if page_status == Page.SafetyStatus.REVIEW else SAFETY_BLOCK_REASON


def skip_media(
    media: Media, reason: str, *, fetched: bool = True, now: datetime | None = None
) -> None:
    """Final state for media we cannot or need not hash (expired link, too large, not media)."""
    now = now or timezone.now()
    media.status = Media.Status.SKIPPED
    media.last_error = reason[:ERROR_MAX_LENGTH]
    media.locked_at = None
    media.next_attempt_at = None
    if fetched:
        media.fetched_at = now
    media.save(update_fields=_FINISH_FIELDS)


def robots_block_media(media: Media, reason: str) -> None:
    """Final state for media its host's robots.txt disallows: nothing was requested.

    Frame hashes the row already has are left alone; only the worker's claim sets this status, and
    a finished row is never claimed again.
    """
    media.status = Media.Status.ROBOTS_BLOCKED
    media.last_error = reason[:ERROR_MAX_LENGTH]
    media.locked_at = None
    media.next_attempt_at = None
    media.save(update_fields=_FINISH_FIELDS)


def retry_delay(attempts: int) -> timedelta:
    return min(RETRY_BASE_DELAY * (1 << max(attempts - 1, 0)), RETRY_MAX_DELAY)


def fail_media(
    media: Media,
    error: str,
    *,
    retry: bool = True,
    fetched: bool = True,
    now: datetime | None = None,
) -> None:
    """Record an error; retry with exponential backoff until MAX_ATTEMPTS, then FAILED."""
    now = now or timezone.now()
    media.last_error = error[:ERROR_MAX_LENGTH]
    media.locked_at = None
    if fetched:
        media.fetched_at = now
    if retry and media.attempts < MAX_ATTEMPTS:
        media.status = Media.Status.PENDING
        media.next_attempt_at = now + retry_delay(media.attempts)
    else:
        media.status = Media.Status.FAILED
        media.next_attempt_at = None
    media.save(update_fields=_FINISH_FIELDS)


def same_host_regex(host: str) -> str:
    """Match every media URL served by the same server as `host`.

    Keyed like the crawl runtime's host slots (`core.http.host_key`), so `example.test` and
    `www.example.test` — one server for politeness, one slot, ADR 0030 § 2 — hold each other.
    That matters because `HostBusyError` names the slot *key*, which never carries `www.`: keying
    this on the raw host let every `www.` media row be claimed and time out in the slot one by one
    instead of being held together.
    """
    return rf"^https?://(www\.)?{re.escape(host_key(host))}(:[0-9]+)?([/?#]|$)"


def hold_media(media: Media, reason: str, until: datetime) -> None:
    """Put this row back untouched and out of reach until `until`: nothing was sent.

    The single-row half of `defer_media`, on its own for a hold that is about this row and no
    other — a suppressed media URL shape (ADR 0041), which is scoped to a site and a shape and not
    to a host. PENDING with the attempt refunded, never SKIPPED or FAILED: the row is claimed again
    by itself once the clock says so, so rehabilitating a shape needs nothing re-queued by hand.
    """
    media.status = Media.Status.PENDING
    media.attempts = max(media.attempts - 1, 0)
    media.locked_at = None
    media.next_attempt_at = until
    media.last_error = reason[:ERROR_MAX_LENGTH]
    media.save(update_fields=("status", "attempts", "locked_at", "next_attempt_at", "last_error"))


def defer_media(
    media: Media,
    reason: str,
    *,
    host: str,
    until: datetime,
    site_id: int | None = None,
) -> int:
    """A request budget is used up (issue #74) or a host slot stayed taken (ADR 0030): nothing was
    sent. Put this row back untouched (attempt refunded) and hold every other *claimable* pending
    row on the same host until `until` — rows of `site_id` for an origin budget, of every site for
    a shared CDN host budget — so the worker does not claim them one by one only to be refused
    again. Returns how many other rows were held.

    Claimable is what the hold is for, so it is filtered exactly as `claim_media` filters: a row
    on an inactive page, an ineligible row or one on a page that is not safety-clear is never
    handed out and holding it changes nothing. That is not cosmetic — on 2026-09-18 the live
    index had 256,769 media rows, 85,714 of them pending and 83,287 of those on inactive pages
    (issue #71), so one `HostBusyError` on the busiest CDN sequentially scanned the whole table
    and rewrote 42,140 rows to postpone work nothing was going to claim. A slot's `retry_at` is
    about a second away, and each of the media lane's threads can trigger it.
    """
    hold_media(media, reason, until)
    same_host = (
        claimable_media()
        .filter(url__iregex=same_host_regex(host))
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lt=until))
        .exclude(pk=media.pk)
    )
    if site_id is not None:
        same_host = same_host.filter(page__site_id=site_id)
    return same_host.update(next_attempt_at=until)


def recover_stale_media(*, now: datetime | None = None) -> int:
    """Put media left RUNNING by a crashed worker back into the queue.

    Media that already used MAX_ATTEMPTS is FAILED instead, so a row that kills its worker is not
    retried for ever. Returns how many rows were re-queued.
    """
    now = now or timezone.now()
    stale = Media.objects.filter(status=Media.Status.RUNNING, locked_at__lt=now - STALE_LOCK_AFTER)
    stale.filter(attempts__gte=MAX_ATTEMPTS).update(
        status=Media.Status.FAILED,
        locked_at=None,
        next_attempt_at=None,
        last_error=f"abandoned: the worker stopped while processing it {MAX_ATTEMPTS} times",
    )
    return stale.update(status=Media.Status.PENDING, locked_at=None)
