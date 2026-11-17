"""Deterministic majority-based assignments, without database access."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations


@dataclass(frozen=True)
class Edge:
    page_a: int
    page_b: int
    evidence: str
    min_distance: int | None = None


@dataclass(frozen=True)
class Membership:
    cluster: int
    page: int
    status: str = "proposed"
    evidence: str = "manual"
    min_distance: int | None = None


def assignments(
    edges: Iterable[Edge],
    existing: Iterable[Membership],
    different: Iterable[tuple[int, int]] = (),
) -> list[Membership]:
    """Return additions only; negative cluster IDs identify new clusters.

    Existing clusters never automatically merge. Revisit unassigned pages after each
    admission, since a new neighbour can establish a majority. Confirmed/rejected
    pairs are different globally; co-rejected pairs cannot reuse either rejected
    evidence kind, but may be reconsidered with a different kind.

    A page only ever considers the clusters one of its own neighbours is in: a strict
    majority needs at least one linked member, so a cluster with no neighbour of the page
    can never admit it. Walking every cluster for every page instead made this the
    heaviest recurring job on the live index (measured 2026-09-17: 490 s of `build_clusters`'
    501 s at 9,603 edges and ~1,750 clusters, growing with pages x clusters x passes).
    `matching/tests/test_clustering.py` pins the result against the exhaustive walk.
    """
    members = list(existing)
    active: dict[int, set[int]] = defaultdict(set)
    occupied: set[int] = set()
    rejected = {(m.cluster, m.page) for m in members if m.status == "rejected"}
    # Which clusters a page is an active member of: the index behind "only its neighbours'
    # clusters", kept up to date by `add`.
    clusters_of: dict[int, set[int]] = defaultdict(set)
    for m in members:
        if m.status != "rejected":
            active[m.cluster].add(m.page)
            clusters_of[m.page].add(m.cluster)
            occupied.add(m.page)
    blocked = {frozenset(pair) for pair in different}
    decisions: dict[int, list[Membership]] = defaultdict(list)
    for member in members:
        decisions[member.cluster].append(member)
    # A plain dict, not a defaultdict: `allowed` asks about pairs that have no rejected
    # evidence on every pass, and a defaultdict would remember every one of them.
    rejected_evidence: dict[frozenset[int], set[str]] = {}
    for cluster_members in decisions.values():
        for first, second in combinations(cluster_members, 2):
            pair = frozenset((first.page, second.page))
            if {first.status, second.status} == {"confirmed", "rejected"}:
                blocked.add(pair)
            elif first.status == second.status == "rejected":
                rejected_evidence.setdefault(pair, set()).update((first.evidence, second.evidence))
    neighbours: dict[int, dict[int, Edge]] = defaultdict(dict)
    for edge in sorted(edges, key=lambda e: (e.evidence, e.min_distance or 0)):
        if edge.page_a == edge.page_b or edge.evidence in rejected_evidence.get(
            frozenset((edge.page_a, edge.page_b)), ()
        ):
            continue
        neighbours[edge.page_a].setdefault(edge.page_b, edge)
        neighbours[edge.page_b].setdefault(edge.page_a, edge)
    result: list[Membership] = []
    # `neighbours` gains no key below, so both orders are computed once instead of per pass.
    pages = sorted(neighbours)
    linked_order = {page: sorted(linked) for page, linked in neighbours.items()}

    def allowed(page: int, cluster: int, evidence: str) -> bool:
        return (cluster, page) not in rejected and all(
            frozenset((page, other)) not in blocked
            and evidence not in rejected_evidence.get(frozenset((page, other)), ())
            for other in active[cluster]
        )

    def add(page: int, cluster: int, edge: Edge) -> None:
        result.append(Membership(cluster, page, "proposed", edge.evidence, edge.min_distance))
        active[cluster].add(page)
        clusters_of[page].add(cluster)
        occupied.add(page)

    next_cluster = min([0, *active]) - 1
    while True:
        changed = False
        for page in pages:
            if page in occupied:
                continue
            linked_pages = neighbours[page]
            candidates = {
                cluster for other in linked_pages for cluster in clusters_of.get(other, ())
            }
            for cluster in sorted(candidates, reverse=True):
                linked = active[cluster] & linked_pages.keys()
                if len(linked) * 2 > len(active[cluster]) and allowed(
                    page, cluster, linked_pages[min(linked)].evidence
                ):
                    add(page, cluster, linked_pages[min(linked)])
                    changed = True
                    break
        if changed:
            continue
        seed = next(
            (
                (a, b, neighbours[a][b])
                for a in pages
                if a not in occupied
                for b in linked_order[a]
                if b not in occupied and a < b and frozenset((a, b)) not in blocked
            ),
            None,
        )
        if seed is None:
            return result
        a, b, edge = seed
        add(a, next_cluster, edge)
        add(b, next_cluster, edge)
        next_cluster -= 1
