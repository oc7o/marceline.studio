"""The frame-embedding pilot, exactly as it was pre-registered (ADR 0037).

Three stages, and an exit criterion frozen in code so nobody can move it after seeing the numbers:

1. **plan** — the bounded page set, the request cost and whether the reference side is ready.
   Writes nothing, requests nothing.
2. **fetch** — re-fetch that set's media through `index.media_fetch` (the one path allowed to ask
   for media bytes, which re-reads the safety gate immediately before every request), embed in
   memory and store **only** the sketch on the frame rows that are already there. The bytes are
   discarded, no PDQ hash is rewritten, no media status changes. A picture whose PDQ hash no
   longer matches the stored one is a *different* picture — the site re-encoded it — and is
   counted and skipped rather than described by a sketch that belongs to another frame.
3. **measure** — the three pre-registered numbers and the verdict.

The criterion, from ADR 0037: adopt only if the embedding brings **>= 50 %** of the cross-site
frame pairs PDQ loses into a mutual top-10, while adding **<= 5** candidate originals per page to
review, and at least **5** of the near-miss pages reach the review band. If recall clears the bar
and precision does not, the fallback is the narrower design (embedding only inside a reference's
own shortlist) — not a different criterion.

Lives in the project package because one pilot run needs `matching`, `index` and the safety gate
together, like `marceline.scheduler` and `marceline.safety_scan`.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from django.db import transaction

from catalog.models import Site
from core.http import BudgetExhaustedError, PoliteClient, RequestRefusedError
from index.media_fetch import MediaFetchDenied, fetch_media
from index.models import FrameHash, Media, Page
from matching import embedding, embedding_candidates
from matching.hamming import HashIndex
from matching.media_hash import hash_media_bytes
from matching.media_robots import MediaRobots
from matching.media_worker import IMAGE_MAX_BYTES, MAX_BYTES, media_delay_s
from matching.models import Reference, ReferenceFrameHash, ReferenceMatch
from matching.pdq import MIN_QUALITY
from matching.sketch_index import SketchIndex

logger = logging.getLogger(__name__)

# --- the pre-registered exit criterion (ADR 0037). Do not tune these to a result. ---
#: Cross-site frame pairs PDQ loses that must become mutual top-10 neighbours.
MIN_LOST_PAIR_RECALL = 0.50
#: Candidate originals a page may add to the review queue.
MAX_CANDIDATES_PER_PAGE = 5.0
#: Near-miss pages that must reach the review band.
MIN_NEAR_MISS_REACHED = 5

#: PDQ's production match threshold; a pair beyond it is one PDQ loses.
PDQ_THRESHOLD = 31
#: The cross-site search radius of the research (`second-signal.md` §1.2), wider than the matcher's.
CROSS_SITE_RADIUS = 48
#: The near-miss band of `second-signal.md` §1.1: close enough to be real, too far to match.
NEAR_MISS_MIN = 32
NEAR_MISS_MAX = 64
#: Share of a site's own daily media budget one pilot run may spend (ADR 0037's "no site above
#: 10 % of its own").
SITE_BUDGET_SHARE = 0.10


@dataclass(frozen=True)
class Plan:
    """The bounded page set, per site, with what fetching it would cost."""

    near_miss_pages: list[int]
    cross_site_pages: list[int]
    media_per_site: dict[str, int]
    budget_per_site: dict[str, int]
    reference_moments: int
    references_embedded: int
    references_total: int

    @property
    def pages(self) -> list[int]:
        return sorted(set(self.near_miss_pages) | set(self.cross_site_pages))

    @property
    def media(self) -> int:
        return sum(self.media_per_site.values())

    def over_budget(self) -> dict[str, tuple[int, int]]:
        """Sites where the plan would exceed its share of the day's media budget."""
        return {
            domain: (count, int(self.budget_per_site[domain] * SITE_BUDGET_SHARE))
            for domain, count in self.media_per_site.items()
            if count > self.budget_per_site.get(domain, 0) * SITE_BUDGET_SHARE
        }

    def lines(self) -> list[str]:
        out = [
            f"Reference side: {self.references_embedded} of {self.references_total} originals "
            f"carry sketches ({self.reference_moments} moments).",
            f"Page side: {len(self.near_miss_pages)} near-miss pages "
            f"({NEAR_MISS_MIN}-{NEAR_MISS_MAX} bits from an original), "
            f"{len(self.cross_site_pages)} pages of cross-site pairs, "
            f"{len(self.pages)} distinct pages, {self.media} media to re-fetch.",
        ]
        for domain, count in sorted(self.media_per_site.items(), key=lambda kv: -kv[1]):
            budget = self.budget_per_site.get(domain, 0)
            share = f"{count / budget:.1%}" if budget else "no budget recorded"
            out.append(f"  {domain}: {count} media, {share} of its daily media budget")
        for domain, (count, allowed) in sorted(self.over_budget().items()):
            out.append(f"  ! {domain}: {count} media is over this run's share ({allowed})")
        if not self.references_embedded:
            out.append(
                "! no original carries sketches yet: re-fingerprint them on the operator's Mac "
                "(`client/fingerprint.py --embeddings`) and import the files first"
            )
        return out


def near_miss_pages(min_quality: int = MIN_QUALITY) -> dict[int, int]:
    """Active pages whose best frame is `NEAR_MISS_MIN`..`NEAR_MISS_MAX` bits from any original.

    The population `second-signal.md` §1.1 measured at 31 pages: close enough that 28 of them
    carry a creator link, too far for the matcher to ever see them.
    """
    reference = list(
        ReferenceFrameHash.objects.filter(quality__gte=min_quality)
        .order_by("pk")
        .values_list("pk", "pdq")
    )
    frames = list(
        FrameHash.objects.filter(quality__gte=min_quality, media__page__active=True)
        .order_by("pk")
        .values_list("media__page_id", "pdq")
    )
    if not reference or not frames:
        return {}
    index = HashIndex.from_pairs([(pk, bytes(pdq)) for pk, pdq in reference])
    best: dict[int, int] = {}
    positions, _, distances = index.pairs_within([bytes(pdq) for _, pdq in frames], NEAR_MISS_MAX)
    for position, distance in zip(positions.tolist(), distances.tolist(), strict=True):
        page_id = int(frames[position][0])
        if distance < best.get(page_id, NEAR_MISS_MAX + 1):
            best[page_id] = int(distance)
    return {page: value for page, value in best.items() if value >= NEAR_MISS_MIN}


@dataclass(frozen=True)
class CrossSitePair:
    """Two page frames on different sites whose pictures PDQ says are the same, or nearly."""

    first_frame: int
    second_frame: int
    first_page: int
    second_page: int
    distance: int


def cross_site_pairs(
    min_quality: int = MIN_QUALITY, radius: int = CROSS_SITE_RADIUS
) -> list[CrossSitePair]:
    """Frame pairs of different files on different sites within `radius` bits.

    The population of `second-signal.md` §1.2 (3,394 pairs at radius 48, median 28 bits, 44 %
    of them beyond the matcher's threshold). Same-file pairs are excluded: two sites serving the
    same bytes are a mirror, not two encodes, and are distance 0 by construction.
    """
    rows = list(
        FrameHash.objects.filter(quality__gte=min_quality, media__page__active=True)
        .order_by("pk")
        .values_list("pk", "media__page_id", "media__page__site_id", "media__sha256", "pdq")
    )
    if not rows:
        return []
    index = HashIndex.from_pairs([(row[0], bytes(row[4])) for row in rows])
    by_id = {row[0]: row for row in rows}
    first_ids, second_ids, distances = index.neighbor_pairs(radius)
    pairs: list[CrossSitePair] = []
    for a, b, distance in zip(
        first_ids.tolist(), second_ids.tolist(), distances.tolist(), strict=True
    ):
        left, right = by_id[a], by_id[b]
        if left[2] == right[2] or (left[3] and left[3] == right[3]):
            continue
        pairs.append(CrossSitePair(a, b, int(left[1]), int(right[1]), int(distance)))
    return pairs


def plan() -> Plan:
    """The pre-registered page set and its cost. Reads only; requests nothing."""
    near = near_miss_pages()
    pairs = cross_site_pairs()
    cross = sorted({pair.first_page for pair in pairs} | {pair.second_page for pair in pairs})
    pages = sorted(set(near) | set(cross))
    media = (
        Media.objects.filter(page_id__in=pages, status=Media.Status.DONE)
        .exclude(kind=Media.Kind.CAPTURE)
        .exclude(eligible_at__isnull=True)
        .values_list("page__site__domain", flat=True)
    )
    per_site: dict[str, int] = {}
    for domain in media:
        per_site[domain] = per_site.get(domain, 0) + 1
    budgets = dict(
        Site.objects.filter(domain__in=per_site).values_list("domain", "daily_media_request_budget")
    )
    return Plan(
        near_miss_pages=sorted(near),
        cross_site_pages=cross,
        media_per_site=per_site,
        budget_per_site=budgets,
        reference_moments=ReferenceFrameHash.objects.filter(
            embedding__isnull=False, dihedral=0
        ).count(),
        references_embedded=Reference.objects.exclude(embedding_pipeline="").count(),
        references_total=Reference.objects.count(),
    )


@dataclass
class FetchReport:
    """What the bounded re-fetch did. Every number is a real outcome, never an estimate."""

    requested: int = 0
    embedded_frames: int = 0
    embedded_media: int = 0
    gone: int = 0
    denied: int = 0
    changed: int = 0
    errors: int = 0
    skipped_no_frames: int = 0
    budget_stop: str = ""
    per_site: dict[str, int] = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = [
            f"Re-fetched {self.requested} media: {self.embedded_media} embedded "
            f"({self.embedded_frames} frames), {self.gone} gone (404/expired), "
            f"{self.changed} re-encoded since indexing (skipped), {self.denied} refused by the "
            f"safety gate or robots, {self.errors} errors, "
            f"{self.skipped_no_frames} without stored frames."
        ]
        if self.budget_stop:
            out.append(f"Stopped early: {self.budget_stop}")
        out.extend(
            f"  {domain}: {count} requests"
            for domain, count in sorted(self.per_site.items(), key=lambda kv: -kv[1])
        )
        return out


def _host_delay(robots: MediaRobots, media: Media, delay_s: float) -> Callable[[], float]:
    """The media worker's rule: the site's delay, raised by the host's robots Crawl-delay."""

    def delay() -> float:
        return max(delay_s, robots.crawl_delay_s(media.url))

    return delay


def _store_sketches(media: Media, sketches: dict[int, bytes]) -> int:
    """Write sketches onto existing frame rows. Nothing else about the frame is touched."""
    written = 0
    with transaction.atomic():
        for frame_index, sketch in sketches.items():
            written += FrameHash.objects.filter(media=media, frame_index=frame_index).update(
                embedding=sketch, embedding_pipeline=embedding.EMBEDDING_PIPELINE
            )
    return written


def embed_media(
    media: Media, data: bytes, *, model_path: str | None = None
) -> tuple[dict[int, bytes], int]:
    """({frame_index: sketch}, frames whose PDQ hash changed) for freshly fetched bytes.

    The sketch is only kept where the re-decoded picture hashes **exactly** as the stored frame
    does. Anything else means the site is serving a different picture at that URL now, and a
    sketch of it would describe a moment the stored PDQ hash never saw.
    """
    hashed = hash_media_bytes(
        str(media.kind), data, embed=True, model_path=model_path, crop_bars=False
    )
    stored = {
        int(index): bytes(pdq)
        for index, pdq in FrameHash.objects.filter(media=media).values_list("frame_index", "pdq")
    }
    sketches: dict[int, bytes] = {}
    changed = 0
    for frame in hashed.frames:
        current = stored.get(frame.frame_index)
        if current is None:
            continue
        if current != frame.hash:
            changed += 1
            continue
        if frame.embedding is not None:
            sketches[frame.frame_index] = frame.embedding
    return sketches, changed


def fetch_and_embed(
    client: PoliteClient,
    media_rows: Sequence[Media],
    *,
    robots: MediaRobots,
    model_path: str | None = None,
) -> FetchReport:
    """Re-fetch each media and store its frames' sketches. Bytes never leave memory.

    Every request goes through `index.media_fetch.fetch_media`, which re-reads the page's safety
    status, score and eligibility from the database immediately before asking — the pilot gets no
    shortcut around the gate, and a page flagged since indexing is simply refused. The media
    host's own robots.txt is checked before every attempt and redirect hop (`robots`), and the
    request waits for that host's turn like the media worker's would.
    """
    report = FetchReport()
    for media in media_rows:
        domain = media.page.site.domain
        report.per_site[domain] = report.per_site.get(domain, 0) + 1
        report.requested += 1
        if not FrameHash.objects.filter(media=media).exists():
            report.skipped_no_frames += 1
            continue
        delay_s = media_delay_s(media)
        try:
            response = fetch_media(
                client,
                media,
                min_delay_s=_host_delay(robots, media, delay_s),
                max_bytes=MAX_BYTES.get(media.kind, IMAGE_MAX_BYTES),
                check=robots.check_for(media, delay_s, client=client),
            )
        except BudgetExhaustedError as exhausted:
            # A budget is a hard stop, not a row to skip: continuing would only keep hitting it.
            # The remaining media are still there, so the next run picks up where this one left off.
            report.requested -= 1
            report.per_site[domain] -= 1
            report.budget_stop = (
                f"{exhausted.scope} budget exhausted on {exhausted.host}; resume after "
                f"{exhausted.resume_at:%H:%M} UTC"
            )
            break
        except MediaFetchDenied:
            # The safety gate, re-read immediately before the request. Nothing was sent.
            report.denied += 1
            continue
        except RequestRefusedError:
            # The media host's robots.txt, an unroutable address, a busy host slot: no request.
            report.denied += 1
            continue
        except Exception as exc:  # network trouble: counted, never fatal
            logger.info("pilot: media %s not fetched (%s)", media.pk, type(exc).__name__)
            report.errors += 1
            continue
        if response.status != 200 or not response.body:
            report.gone += 1
            continue
        try:
            sketches, changed = embed_media(media, response.body, model_path=model_path)
        except Exception as exc:
            logger.info("pilot: media %s not embedded (%s)", media.pk, type(exc).__name__)
            report.errors += 1
            continue
        report.changed += changed
        if sketches:
            report.embedded_frames += _store_sketches(media, sketches)
            report.embedded_media += 1
    return report


@dataclass(frozen=True)
class Measurement:
    """The three pre-registered numbers, and whether they clear the frozen bar."""

    lost_pairs: int
    lost_pairs_mutual: int
    embedded_pages: int
    candidate_pairs: int
    near_miss_total: int
    near_miss_reached: int
    pipeline: str

    @property
    def lost_pair_recall(self) -> float:
        return self.lost_pairs_mutual / self.lost_pairs if self.lost_pairs else 0.0

    @property
    def candidates_per_page(self) -> float:
        return self.candidate_pairs / self.embedded_pages if self.embedded_pages else 0.0

    @property
    def recall_passes(self) -> bool:
        return bool(self.lost_pairs) and self.lost_pair_recall >= MIN_LOST_PAIR_RECALL

    @property
    def precision_passes(self) -> bool:
        return self.candidates_per_page <= MAX_CANDIDATES_PER_PAGE

    @property
    def findings_passes(self) -> bool:
        return self.near_miss_reached >= MIN_NEAR_MISS_REACHED

    @property
    def adopt(self) -> bool:
        return self.recall_passes and self.precision_passes and self.findings_passes

    def verdict(self) -> str:
        if self.adopt:
            return "ADOPT: every pre-registered criterion is met."
        if self.recall_passes and not self.precision_passes:
            return (
                "FALLBACK: recall clears the bar and precision does not — run the embedding only "
                "inside a reference's own shortlist (pages already linked by duration, creator or "
                "cluster), as ADR 0037 pre-registered. Do not raise the precision bar."
            )
        if not self.recall_passes:
            return (
                "DROP: the second signal does not reach the pairs PDQ misses on our own data. "
                "160 MB of resident set per hashing process is not worth that."
            )
        return "DO NOT ADOPT: the near-miss criterion is not met."

    def lines(self) -> list[str]:
        return [
            f"Pipeline: {self.pipeline}",
            f"1. Cross-site pairs PDQ loses (> {PDQ_THRESHOLD} bits) with sketches on both "
            f"sides: {self.lost_pairs}; mutual top-10: {self.lost_pairs_mutual} "
            f"= {self.lost_pair_recall:.1%} "
            f"(bar: >= {MIN_LOST_PAIR_RECALL:.0%}) "
            f"{'PASS' if self.recall_passes else 'FAIL'}",
            f"2. Candidate originals per embedded page: {self.candidate_pairs} over "
            f"{self.embedded_pages} pages = {self.candidates_per_page:.2f} "
            f"(bar: <= {MAX_CANDIDATES_PER_PAGE:.0f}) "
            f"{'PASS' if self.precision_passes else 'FAIL'}",
            f"3. Near-miss pages reaching the review band: {self.near_miss_reached} of "
            f"{self.near_miss_total} (bar: >= {MIN_NEAR_MISS_REACHED}) "
            f"{'PASS' if self.findings_passes else 'FAIL'}",
            self.verdict(),
        ]


def measure(pipeline: str = embedding.EMBEDDING_PIPELINE) -> Measurement:
    """The pilot's three numbers, read from what the fetch and the candidate refresh produced."""
    sketches = dict(
        FrameHash.objects.filter(
            embedding__isnull=False, embedding_pipeline=pipeline, media__page__active=True
        ).values_list("pk", "embedding")
    )
    # The query excludes NULL, so every value is real bytes (a memoryview from psycopg).
    values = {pk: bytes(value) for pk, value in sketches.items()}  # type: ignore[arg-type]
    pairs = [
        pair
        for pair in cross_site_pairs()
        if pair.distance > PDQ_THRESHOLD
        and pair.first_frame in values
        and pair.second_frame in values
    ]
    mutual = 0
    if pairs and len(values) > 1:
        index = SketchIndex(list(values), list(values.values()))
        ids, _ = index.nearest(list(values.values()), embedding_candidates.DEFAULT_TOP_K + 1)
        order = {pk: position for position, pk in enumerate(values)}
        neighbours = {pk: set(ids[order[pk]].tolist()) - {pk} for pk in values}
        mutual = sum(
            1
            for pair in pairs
            if pair.second_frame in neighbours[pair.first_frame]
            and pair.first_frame in neighbours[pair.second_frame]
        )
    embedded_pages = (
        Page.objects.filter(
            media__hashes__embedding__isnull=False, media__hashes__embedding_pipeline=pipeline
        )
        .distinct()
        .count()
    )
    candidates = ReferenceMatch.objects.filter(
        candidate_source=ReferenceMatch.CandidateSource.EMBEDDING
    )
    near = set(near_miss_pages())
    return Measurement(
        lost_pairs=len(pairs),
        lost_pairs_mutual=mutual,
        embedded_pages=embedded_pages,
        candidate_pairs=candidates.count(),
        near_miss_total=len(near),
        near_miss_reached=candidates.filter(page_id__in=near, band="review").count(),
        pipeline=pipeline,
    )
