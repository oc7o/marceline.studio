import random
from collections import defaultdict
from collections.abc import Iterable
from itertools import combinations

from matching.clustering import Edge, Membership, assignments


def exhaustive_assignments(
    edges: Iterable[Edge],
    existing: Iterable[Membership],
    different: Iterable[tuple[int, int]] = (),
) -> list[Membership]:
    """The straightforward reading of the admission rule: every page against every cluster.

    `assignments` only offers a page the clusters one of its neighbours is in, which is what
    makes it fast (see its docstring). This is the specification that shortcut must not change;
    `test_shortcut_matches_exhaustive_walk` compares them on randomized inputs.
    """
    members = list(existing)
    active: dict[int, set[int]] = defaultdict(set)
    occupied: set[int] = set()
    rejected = {(m.cluster, m.page) for m in members if m.status == "rejected"}
    for m in members:
        if m.status != "rejected":
            active[m.cluster].add(m.page)
            occupied.add(m.page)
    blocked = {frozenset(pair) for pair in different}
    decisions: dict[int, list[Membership]] = defaultdict(list)
    for member in members:
        decisions[member.cluster].append(member)
    rejected_evidence: dict[frozenset[int], set[str]] = defaultdict(set)
    for cluster_members in decisions.values():
        for first, second in combinations(cluster_members, 2):
            pair = frozenset((first.page, second.page))
            if {first.status, second.status} == {"confirmed", "rejected"}:
                blocked.add(pair)
            elif first.status == second.status == "rejected":
                rejected_evidence[pair].update((first.evidence, second.evidence))
    neighbours: dict[int, dict[int, Edge]] = defaultdict(dict)
    for edge in sorted(edges, key=lambda e: (e.evidence, e.min_distance or 0)):
        if (
            edge.page_a == edge.page_b
            or edge.evidence in rejected_evidence[frozenset((edge.page_a, edge.page_b))]
        ):
            continue
        neighbours[edge.page_a].setdefault(edge.page_b, edge)
        neighbours[edge.page_b].setdefault(edge.page_a, edge)
    result: list[Membership] = []

    def allowed(page: int, cluster: int, evidence: str) -> bool:
        return (cluster, page) not in rejected and all(
            frozenset((page, other)) not in blocked
            and evidence not in rejected_evidence[frozenset((page, other))]
            for other in active[cluster]
        )

    def add(page: int, cluster: int, edge: Edge) -> None:
        result.append(Membership(cluster, page, "proposed", edge.evidence, edge.min_distance))
        active[cluster].add(page)
        occupied.add(page)

    next_cluster = min([0, *active]) - 1
    while True:
        changed = False
        for page in sorted(neighbours):
            if page in occupied:
                continue
            for cluster in sorted(active, reverse=True):
                linked = active[cluster] & neighbours[page].keys()
                if len(linked) * 2 > len(active[cluster]) and allowed(
                    page, cluster, neighbours[page][min(linked)].evidence
                ):
                    add(page, cluster, neighbours[page][min(linked)])
                    changed = True
                    break
        if changed:
            continue
        seed = next(
            (
                (a, b, neighbours[a][b])
                for a in sorted(neighbours)
                if a not in occupied
                for b in sorted(neighbours[a])
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


def _random_case(
    rng: random.Random,
) -> tuple[list[Edge], list[Membership], list[tuple[int, int]]]:
    pages = list(range(1, rng.randint(8, 26)))
    kinds = ("pdq", "sha256", "external_link", "mirror", "manual")
    edges = [
        Edge(a, b, rng.choice(kinds), rng.choice([None, 0, 7, 19, 31]))
        for a, b in combinations(pages, 2)
        if rng.random() < 0.22
    ]
    existing = [
        Membership(
            rng.randint(1, 4),
            rng.choice(pages),
            rng.choice(["confirmed", "rejected", "proposed"]),
            rng.choice(kinds),
        )
        for _ in range(rng.randint(0, 8))
    ]
    different = [
        (min(a, b), max(a, b)) for a, b in (rng.sample(pages, 2) for _ in range(rng.randint(0, 3)))
    ]
    return edges, existing, different


def test_shortcut_matches_exhaustive_walk() -> None:
    """Only-my-neighbours' clusters must decide exactly what the full walk decides."""
    rng = random.Random(20260917)
    compared = 0
    for _ in range(200):
        edges, existing, different = _random_case(rng)
        # `existing` may contain a page that is both confirmed and rejected in one cluster;
        # that is a conflict `build_clusters` rejects before calling this, so skip it here.
        decided = [m for m in existing if m.status != "proposed"]
        assert assignments(edges, decided, different) == exhaustive_assignments(
            edges, decided, different
        )
        compared += 1
    assert compared == 200


def test_majority_and_no_chaining() -> None:
    edges = [Edge(1, 2, "pdq"), Edge(2, 3, "pdq")]
    assert {m.page for m in assignments(edges, [])} == {1, 2}
    edges.append(Edge(1, 3, "pdq"))
    assert {m.page for m in assignments(edges, [])} == {1, 2, 3}


def test_existing_clusters_and_human_decisions() -> None:
    existing = [
        Membership(1, 1, "confirmed"),
        Membership(1, 2),
        Membership(2, 3, "confirmed"),
        Membership(2, 4),
        Membership(1, 5, "rejected"),
    ]
    edges = [Edge(a, b, "sha256") for a, b in [(1, 3), (1, 5), (2, 5)]]
    assert assignments(edges, existing) == []
    assert assignments([Edge(6, 7, "manual")], existing, [(6, 7)]) == []


def test_strict_majority_of_four() -> None:
    existing = [Membership(1, p) for p in range(1, 5)]
    edges = [Edge(p, 5, "pdq") for p in (1, 2)]
    assert assignments(edges, existing) == []
    assert [m.page for m in assignments([*edges, Edge(3, 5, "pdq")], existing)] == [5]


def test_rejected_evidence_and_alternative() -> None:
    existing = [Membership(52, p, "rejected", "external_link") for p in (665, 668)]
    links = Edge(665, 668, "external_link")
    assert assignments([links], existing) == []
    result = assignments([links, Edge(665, 668, "pdq", 0)], existing)
    assert {m.page for m in result} == {665, 668}
    assert {m.evidence for m in result} == {"pdq"}


def test_rejection_blocks_admission_through_other_pages() -> None:
    existing = [
        Membership(52, 1, "confirmed"),
        Membership(52, 3, "rejected"),
        Membership(53, 3, "confirmed"),
    ]
    edges = [Edge(a, b, "pdq") for a, b in [(1, 2), (1, 4), (2, 4), (2, 3), (4, 3)]]
    result = assignments(edges, existing)
    assert all(m.cluster != 52 or m.page != 3 for m in result)
    assert all(m.cluster != 53 or m.page != 1 for m in result)
