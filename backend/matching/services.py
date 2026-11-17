import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations
from urllib.parse import urlsplit

from django.db import connection, transaction
from django.utils import timezone

from index.models import FrameHash, Media, Page
from matching.clustering import Edge, Membership, assignments
from matching.duplicates import (
    DEFAULT_MAX_DISTANCE,
    DuplicateCandidate,
    find_duplicate_pages,
    pair_stats,
)
from matching.evaluate import LabeledPair
from matching.models import Cluster, ClusterMember, PairLabel
from matching.pdq import MIN_QUALITY

MAX_EVIDENCE_PAGES = 5
MIRROR_POSTER_PATH_PATTERN = re.compile(
    r"/(imgs/[^/]+/[^/]+/[0-9a-fA-F]{15})_(?:main|[0-9]+)\.jpg$"
)
# site03's CDN rotates a per-asset token and a 10-digit expiry in otherwise stable paths:
# `/tkn/{x}/{y}/{vid|img}/{token}/{expiry}/{uploader}/{post}/{file}` (see matching.media).
TOKENIZED_MEDIA_PATH_PATTERN = re.compile(
    r"^/tkn/([^/]+/[^/]+/(?:vid|img))/[^/]+/[0-9]{10}/([^/]+/[^/]+/[^/]+)$"
)
TOKENIZED_MEDIA_HOST = "cdn.site03.example"


@dataclass(frozen=True)
class MediaKey:
    """Host-independent identity of one image or clip on a known mirror CDN.

    `needle` is a substring of every URL with this key (an SQL prefilter)."""

    value: str
    needle: str


def _http_parts(url: str) -> tuple[str, str] | None:
    """(lowercase host, path) of an http(s) URL, else None."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return parsed.hostname.lower(), parsed.path


def mirror_key(url: str) -> str | None:
    """Page-level mirror key `imgs/{a}/{b}/{15-hex}` of a poster/frame URL (any host, prefix,
    query string or `_main` / `_N` suffix), else None."""
    parts = _http_parts(url)
    if parts is None or not (match := MIRROR_POSTER_PATH_PATTERN.search(parts[1])):
        return None
    return match.group(1)


def media_content_key(url: str) -> MediaKey | None:
    """Key under which known mirror CDNs serve identical bytes, else None.

    - site02/site04 mirrors: the mirror key *with* its `_main` / `_N` suffix (each suffix is
      a different image), independent of host, prefix and query string.
    - site03 CDN (cdn.site03.example): the path without the rotating token and expiry.
    Generic paths are never keys: unrelated sites on the same video CMS reuse the same `videos_screenshots` ids.
    """
    parts = _http_parts(url)
    if parts is None:
        return None
    host, path = parts
    if match := MIRROR_POSTER_PATH_PATTERN.search(path):
        return MediaKey(f"mirror:{match.group(0)[1:]}", match.group(0))
    if (host == TOKENIZED_MEDIA_HOST or host.endswith("." + TOKENIZED_MEDIA_HOST)) and (
        match := TOKENIZED_MEDIA_PATH_PATTERN.match(path)
    ):
        return MediaKey(f"tokenized:{match.group(1)}/{match.group(2)}", f"/{match.group(2)}")
    return None


def mirror_groups() -> list[set[int]]:
    """Recognized poster/frame keys, independent of CDN host, prefix or query string."""
    groups: dict[str, set[int]] = defaultdict(set)
    for page, url in Media.objects.filter(
        kind__in=(Media.Kind.POSTER, Media.Kind.FRAME), page__active=True
    ).values_list("page_id", "url"):
        if key := mirror_key(url):
            groups[key].add(page)
    return [pages for pages in groups.values() if 1 < len(pages) <= MAX_EVIDENCE_PAGES]


def label_pair(
    first: Page, second: Page, label: PairLabel.Label, min_distance: int | None = None
) -> PairLabel:
    """Create or update the label for two pages, storing the pair in canonical order."""
    if first.pk == second.pk:
        raise ValueError("Cannot label a page against itself")
    page_a, page_b = (first, second) if first.pk < second.pk else (second, first)
    pair, _ = PairLabel.objects.update_or_create(
        page_a=page_a,
        page_b=page_b,
        defaults={"label": label, "min_distance": min_distance},
    )
    return pair


def load_page_hashes(
    page_ids: Iterable[int] | None = None, min_quality: int = MIN_QUALITY
) -> dict[int, list[bytes]]:
    """PDQ hashes of usable frames grouped by page, in media/frame order.

    Without `page_ids`, only active pages (issue #71): duplicates and clusters never consider
    inactive ones. Named pages (human labels, evaluation) are loaded whatever their state.
    """
    rows = FrameHash.objects.filter(quality__gte=min_quality)
    if page_ids is not None:
        rows = rows.filter(media__page_id__in=list(page_ids))
    else:
        rows = rows.filter(media__page__active=True)
    hashes: dict[int, list[bytes]] = defaultdict(list)
    for page_id, pdq in rows.order_by("media__page_id", "media_id", "frame_index").values_list(
        "media__page_id", "pdq"
    ):
        hashes[page_id].append(bytes(pdq))
    return dict(hashes)


def find_candidates(
    max_distance: int = DEFAULT_MAX_DISTANCE,
    min_score: float = 0.0,
    min_quality: int = MIN_QUALITY,
    cross_site_only: bool = False,
) -> list[DuplicateCandidate]:
    candidates = find_duplicate_pages(
        load_page_hashes(min_quality=min_quality), max_distance, min_score
    )
    if not cross_site_only:
        return candidates
    page_ids = {page_id for c in candidates for page_id in (c.page_a, c.page_b)}
    site_of = dict(Page.objects.filter(pk__in=page_ids).values_list("pk", "site_id"))
    return [c for c in candidates if site_of[c.page_a] != site_of[c.page_b]]


def propose_pairs(candidates: Iterable[DuplicateCandidate]) -> int:
    """Queue candidates for review as UNSURE labels. Existing labels are human decisions and
    are never overwritten. Returns how many labels were created."""
    created = 0
    for candidate in candidates:
        _, was_created = PairLabel.objects.get_or_create(
            page_a_id=candidate.page_a,
            page_b_id=candidate.page_b,
            defaults={"label": PairLabel.Label.UNSURE, "min_distance": candidate.min_distance},
        )
        created += int(was_created)
    return created


def labeled_pairs(
    max_distance: int = DEFAULT_MAX_DISTANCE, min_quality: int = MIN_QUALITY
) -> list[LabeledPair]:
    """Decided (same/different) labels with distances recomputed from the current hashes."""
    derived: dict[tuple[int, int], str] = {}
    groups: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for cluster, page, status in ClusterMember.objects.exclude(status="proposed").values_list(
        "cluster_id", "page_id", "status"
    ):
        groups[cluster].append((page, status))
    for members in groups.values():
        for (a, sa), (b, sb) in combinations(members, 2):
            if sa == sb == "rejected":
                continue
            key = (min(a, b), max(a, b))
            label = "same" if sa == sb == "confirmed" else "different"
            if derived.get(key) != "different":
                derived[key] = label
    for a, b, label in PairLabel.objects.exclude(label="unsure").values_list(
        "page_a_id", "page_b_id", "label"
    ):
        derived[a, b] = label
    labels = [(a, b, label) for (a, b), label in sorted(derived.items())]
    hashes = load_page_hashes({pid for a, b, _ in labels for pid in (a, b)}, min_quality)
    pairs = []
    for page_a, page_b, label in labels:
        stats = pair_stats(hashes.get(page_a, []), hashes.get(page_b, []), max_distance)
        pairs.append(
            LabeledPair(
                same=label == PairLabel.Label.SAME,
                min_distance=stats.min_distance,
                score=stats.score,
            )
        )
    return pairs


def evidence_edges(
    max_distance: int = DEFAULT_MAX_DISTANCE,
    min_score: float = 0.0,
    min_quality: int = MIN_QUALITY,
) -> list[Edge]:
    """Ignore SHA256/link groups shared by more than MAX_EVIDENCE_PAGES pages.

    External links require Jaccard similarity >= 2/3 across valid non-Telegram
    link sets, including popular links, plus a PDQ candidate for the same pair
    within the configured distance, score and quality thresholds. Duration does
    not establish or veto evidence, including clips.
    """
    edges = [
        Edge(c.page_a, c.page_b, "pdq", c.min_distance)
        for c in find_candidates(max_distance, min_score, min_quality)
    ]
    edges.extend(
        Edge(a, b, "mirror") for pages in mirror_groups() for a, b in combinations(sorted(pages), 2)
    )
    pdq_pairs = {(e.page_a, e.page_b) for e in edges if e.evidence == "pdq"}
    hashes: dict[str, set[int]] = defaultdict(set)
    for page, sha in (
        Media.objects.filter(page__active=True).exclude(sha256="").values_list("page_id", "sha256")
    ):
        hashes[sha].add(page)
    links: dict[str, set[int]] = defaultdict(set)
    page_links: dict[int, set[str]] = defaultdict(set)
    for page, raw in Page.objects.filter(active=True).values_list("pk", "raw_extraction"):
        if not isinstance(raw, dict) or not isinstance(raw.get("external_links"), list):
            continue
        for link in raw["external_links"]:
            if not isinstance(link, str):
                continue
            try:
                parsed = urlsplit(link)
                host = (parsed.hostname or "").lower()
            except ValueError:
                continue
            if parsed.scheme not in ("http", "https") or not host:
                continue
            if any(
                host == domain or host.endswith("." + domain)
                for domain in ("t.me", "telegram.me", "telegram.org", "telegram.dog")
            ):
                continue
            links[link].add(page)
            page_links[page].add(link)
    linked_pairs: set[tuple[int, int]] = set()
    for evidence, groups in (("sha256", hashes), ("external_link", links)):
        for pages in groups.values():
            if len(pages) > MAX_EVIDENCE_PAGES:
                continue
            for a, b in combinations(sorted(pages), 2):
                if evidence == "external_link":
                    if (a, b) in linked_pairs:
                        continue
                    if 3 * len(page_links[a] & page_links[b]) < 2 * len(
                        page_links[a] | page_links[b]
                    ):
                        continue
                    if (a, b) not in pdq_pairs:
                        continue
                    linked_pairs.add((a, b))
                edges.append(Edge(a, b, evidence))
    return edges


def _import_pair_labels() -> None:
    """Human SAME components override proposals; incompatible decisions abort atomically."""
    groups: list[set[int]] = []
    for a, b in PairLabel.objects.filter(label="same").values_list("page_a_id", "page_b_id"):
        group = {a, b}
        touching = [g for g in groups if g & group]
        for other in touching:
            group.update(other)
            groups.remove(other)
        groups.append(group)
    different = list(
        PairLabel.objects.filter(label="different").values_list("page_a_id", "page_b_id")
    )
    for group in groups:
        # Include all confirmed members of touched clusters, preserving their human status.
        while True:
            cluster_ids = set(
                ClusterMember.objects.filter(page_id__in=group, status="confirmed").values_list(
                    "cluster_id", flat=True
                )
            )
            expanded = group | set(
                ClusterMember.objects.filter(
                    cluster_id__in=cluster_ids, status="confirmed"
                ).values_list("page_id", flat=True)
            )
            if expanded == group:
                break
            group = expanded
        if any(a in group and b in group for a, b in different):
            raise ValueError("Conflicting SAME and DIFFERENT human labels")
        candidates = set(
            ClusterMember.objects.filter(page_id__in=group)
            .exclude(status="rejected")
            .values_list("cluster_id", flat=True)
        )
        target = min(cluster_ids or candidates) if (cluster_ids or candidates) else None
        if (
            target is not None
            and ClusterMember.objects.filter(
                cluster_id__in=cluster_ids | {target}, page_id__in=group, status="rejected"
            ).exists()
        ):
            raise ValueError("SAME labels conflict with a rejected cluster membership")
        cluster = Cluster.objects.get(pk=target) if target is not None else Cluster.objects.create()
        for old in (
            ClusterMember.objects.filter(page_id__in=group)
            .exclude(status="rejected")
            .exclude(cluster=cluster)
        ):
            old.cluster = cluster
            old.save(update_fields=("cluster", "updated_at"))
        for page in sorted(group):
            member, created = ClusterMember.objects.get_or_create(
                cluster=cluster,
                page_id=page,
                defaults={"status": "confirmed", "evidence": "manual"},
            )
            if not created and member.status == "proposed":
                member.status = "confirmed"
                member.evidence = "manual"
                member.save(update_fields=("status", "evidence", "updated_at"))
        # A new human decision can invalidate an older automatic proposal.
        incompatible = {b if a in group else a for a, b in different if a in group or b in group}
        ClusterMember.objects.filter(
            cluster=cluster, status="proposed", page_id__in=incompatible
        ).delete()


@transaction.atomic
def build_clusters(
    max_distance: int = DEFAULT_MAX_DISTANCE,
    min_score: float = 0.0,
    min_quality: int = MIN_QUALITY,
    dry_run: bool = False,
    review_max_distance: int | None = None,
) -> tuple[int, int]:
    if review_max_distance is not None and not max_distance <= review_max_distance <= 256:
        raise ValueError("review-max-distance must be between max-distance and 256")
    # Serialize builders, including the first run when no cluster rows exist yet.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(73428109)")
    before = set(Cluster.objects.values_list("pk", flat=True))
    _import_pair_labels()
    for a, b in PairLabel.objects.filter(label="different").values_list("page_a_id", "page_b_id"):
        rows = list(ClusterMember.objects.filter(page_id__in=(a, b)).exclude(status="rejected"))
        if len(rows) == 2 and rows[0].cluster_id == rows[1].cluster_id:
            proposed = [m for m in rows if m.status == "proposed"]
            if not proposed:
                raise ValueError("DIFFERENT label conflicts with confirmed cluster members")
            for proposal in proposed:
                proposal.delete()
    existing = [
        Membership(c, p, s, e, d)
        for c, p, s, e, d in ClusterMember.objects.values_list(
            "cluster_id", "page_id", "status", "evidence", "min_distance"
        )
    ]
    different = list(
        PairLabel.objects.filter(label="different").values_list("page_a_id", "page_b_id")
    )
    additions = assignments(
        evidence_edges(max_distance, min_score, min_quality),
        [m for m in existing if m.status != "proposed"],
        different,
    )
    normal_count = len(additions)
    if review_max_distance is not None:
        # Keep normal assignments fixed; the review band adds PDQ proposals only.
        additions += assignments(
            [
                Edge(c.page_a, c.page_b, "pdq", c.min_distance)
                for c in find_candidates(review_max_distance, min_score, min_quality)
            ],
            [m for m in existing if m.status != "proposed"] + additions,
            different,
        )
    borderline_pages = {m.page for m in additions[normal_count:]}
    # Reuse proposal-only clusters without merging human decisions.
    reusable = {m.cluster for m in existing} - {
        m.cluster for m in existing if m.status != "proposed"
    }
    new_clusters: dict[int, int] = {}
    desired: set[tuple[int, int]] = set()
    proposed_count = 0
    for member in additions:
        cluster_id = member.cluster
        if cluster_id < 0:
            if cluster_id not in new_clusters:
                candidates = sorted(
                    m.cluster for m in existing if m.page == member.page and m.cluster in reusable
                )
                target = candidates[0] if candidates else Cluster.objects.create().pk
                reusable.discard(target)
                new_clusters[cluster_id] = target
            cluster_id = new_clusters[cluster_id]
        desired.add((cluster_id, member.page))
    for old_member in ClusterMember.objects.filter(status="proposed"):
        if (old_member.cluster_id, old_member.page_id) not in desired:
            old_member.delete()
    for member in additions:
        cluster_id = new_clusters.get(member.cluster, member.cluster)
        _, created = ClusterMember.objects.update_or_create(
            cluster_id=cluster_id,
            page_id=member.page,
            defaults={
                "evidence": member.evidence,
                "min_distance": member.min_distance,
                "borderline": member.page in borderline_pages,
            },
        )
        proposed_count += int(created)
    Cluster.objects.filter(pk__in={c for c, _ in desired}).update(updated_at=timezone.now())
    Cluster.objects.filter(members__isnull=True).delete()
    result = len(set(Cluster.objects.values_list("pk", flat=True)) - before), proposed_count
    if dry_run:
        transaction.set_rollback(True)
    return result


def _cluster_edit_lock() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(73428109)")


def _merged_members(members: list[ClusterMember]) -> list[ClusterMember]:
    """Validate against negative decisions before collapsing duplicate pages."""
    by_page: dict[int, list[ClusterMember]] = defaultdict(list)
    for member in members:
        by_page[member.page_id].append(member)
    for rows in by_page.values():
        if {"confirmed", "rejected"} <= {m.status for m in rows}:
            raise ValueError("A page is both CONFIRMED and REJECTED")
    confirmed = {m.page_id for m in members if m.status == "confirmed"}
    if PairLabel.objects.filter(
        label="different", page_a_id__in=confirmed, page_b_id__in=confirmed
    ).exists():
        raise ValueError("DIFFERENT label conflicts with confirmed members")
    decisions: dict[int, set[str]] = defaultdict(set)
    for cluster_id, status in (
        ClusterMember.objects.filter(page_id__in=confirmed)
        .exclude(status="proposed")
        .values_list("cluster_id", "status")
    ):
        decisions[cluster_id].add(status)
    if any({"confirmed", "rejected"} <= statuses for statuses in decisions.values()):
        raise ValueError("Confirmed/rejected pair conflicts with confirmed members")
    rank = {"confirmed": 0, "proposed": 1, "rejected": 2}
    return [min(rows, key=lambda m: (rank[m.status], m.pk)) for rows in by_page.values()]


@transaction.atomic
def merge_clusters(target: Cluster, others: Iterable[Cluster]) -> Cluster:
    """Merge into target atomically, retaining the strongest duplicate row."""
    _cluster_edit_lock()
    ids = {target.pk, *(cluster.pk for cluster in others)}
    list(Cluster.objects.select_for_update().filter(pk__in=ids))
    members = list(ClusterMember.objects.select_for_update().filter(cluster_id__in=ids))
    winners = _merged_members(members)
    ClusterMember.objects.filter(cluster_id__in=ids).exclude(
        pk__in=[m.pk for m in winners]
    ).delete()
    ClusterMember.objects.filter(pk__in=[m.pk for m in winners]).update(
        cluster=target, updated_at=timezone.now()
    )
    Cluster.objects.filter(pk__in=ids).exclude(pk=target.pk).delete()
    return target


@transaction.atomic
def move_member(member: ClusterMember, cluster: Cluster) -> ClusterMember:
    """Move a persisted member, preserving decisions and deleting an empty source."""
    _cluster_edit_lock()
    original = ClusterMember.objects.select_for_update().get(pk=member.pk)
    members = list(
        ClusterMember.objects.select_for_update().filter(cluster=cluster).exclude(pk=member.pk)
    )
    winners = _merged_members([*members, member])
    winner = next(m for m in winners if m.page_id == member.page_id)
    duplicates = [m.pk for m in [*members, member] if m.page_id == member.page_id and m != winner]
    ClusterMember.objects.filter(pk__in=duplicates).delete()
    winner.cluster = cluster
    winner.save()
    Cluster.objects.filter(pk=original.cluster_id, members__isnull=True).delete()
    return winner
