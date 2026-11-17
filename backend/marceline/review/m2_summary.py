"""Structured numbers from `matching.m2.m2_report`.

The report only returns text lines. Rather than re-implementing its ground-truth rules, this module
calls the report and parses its fixed line formats. A format change raises `M2ParseError`, which
the API tests catch.
"""

import re
from dataclasses import dataclass, field

from django.db.models import QuerySet

from matching.m2 import M2_FALSE_MATCH_LIMIT, M2_RECALL_TARGET, m2_report
from matching.models import Reference
from matching.references import STRICT_MAX_DISTANCE

#: The matcher's own strict threshold, so the overview never measures at a distance the
#: matcher does not use.
DEFAULT_MAX_DISTANCE = STRICT_MAX_DISTANCE

_SUMMARY = re.compile(
    r"confirmed leaks=(?P<confirmed>\d+); found strong=(?P<strong>\d+), weak=(?P<weak>\d+), "
    r"linked=(?P<linked>\d+); .*false matches=(?P<false>\d+)/(?P<reviewed>\d+) reviewed .*"
    r"unreviewed suggestions=(?P<suggestions>\d+), matches=(?P<matches>\d+)$"
)
_REFERENCE = re.compile(r"^Reference (?P<id>\d+) ")
_MISS = re.compile(r"^  Miss page (?P<page>\d+): (?P<reason>.*)$")
_LINKED = re.compile(
    r"^  Linked page (?P<page>\d+): active member of cluster (?P<cluster>\d+) "
    r"with strong match page (?P<seed>\d+)$"
)
_EXIT = re.compile(r"^M2 exit: (?P<result>PASS|FAIL);")


class M2ParseError(ValueError):
    """`m2_report` output no longer matches the formats parsed here."""


@dataclass
class M2Numbers:
    confirmed: int = 0
    strong: int = 0
    weak: int = 0
    linked: int = 0
    false: int = 0
    reviewed: int = 0
    suggestions: int = 0
    matches: int = 0

    def recall(self, found: int) -> float | None:
        return found / self.confirmed if self.confirmed else None

    @property
    def false_match_rate(self) -> float | None:
        return self.false / self.reviewed if self.reviewed else None


@dataclass
class M2Miss:
    page_id: int
    reason: str


@dataclass
class M2Linked:
    page_id: int
    cluster_id: int
    via_page_id: int


@dataclass
class M2ReferenceResult:
    numbers: M2Numbers
    misses: list[M2Miss] = field(default_factory=list)
    linked: list[M2Linked] = field(default_factory=list)


@dataclass
class M2Result:
    total: M2Numbers
    references: dict[int, M2ReferenceResult]
    passed: bool
    too_few_to_judge: bool
    max_distance: int
    recall_target: float = M2_RECALL_TARGET
    false_match_limit: float = M2_FALSE_MATCH_LIMIT


def _numbers(line: str) -> M2Numbers:
    found = _SUMMARY.search(line)
    if found is None:
        raise M2ParseError(f"Unrecognized M2 summary: {line!r}")
    return M2Numbers(**{key: int(value) for key, value in found.groupdict().items()})


def parse_m2_lines(lines: list[str], *, max_distance: int) -> M2Result:
    references: dict[int, M2ReferenceResult] = {}
    current: M2ReferenceResult | None = None
    total: M2Numbers | None = None
    exit_line: str | None = None
    for line in lines[1:]:
        if reference := _REFERENCE.match(line):
            current = M2ReferenceResult(numbers=_numbers(line))
            references[int(reference["id"])] = current
        elif miss := _MISS.match(line):
            if current is None:
                raise M2ParseError(f"Miss line outside a reference: {line!r}")
            current.misses.append(M2Miss(page_id=int(miss["page"]), reason=miss["reason"]))
        elif linked := _LINKED.match(line):
            if current is None:
                raise M2ParseError(f"Linked line outside a reference: {line!r}")
            current.linked.append(
                M2Linked(
                    page_id=int(linked["page"]),
                    cluster_id=int(linked["cluster"]),
                    via_page_id=int(linked["seed"]),
                )
            )
        elif line.startswith("Total: "):
            total = _numbers(line)
        elif _EXIT.match(line):
            exit_line = line
        else:
            raise M2ParseError(f"Unrecognized M2 line: {line!r}")
    if total is None or exit_line is None:
        raise M2ParseError("M2 report without total or exit line")
    return M2Result(
        total=total,
        references=references,
        passed=exit_line.startswith("M2 exit: PASS"),
        too_few_to_judge=exit_line.endswith("; too few to judge"),
        max_distance=max_distance,
    )


def m2_summary(
    references: QuerySet[Reference] | None = None, *, max_distance: int = DEFAULT_MAX_DISTANCE
) -> M2Result:
    """Run the read-only M2 report (all references by default) and return its numbers."""
    if references is None:
        references = Reference.objects.select_related("creator").order_by("pk")
    lines = m2_report(references, max_distance=max_distance)
    return parse_m2_lines(lines, max_distance=max_distance)
