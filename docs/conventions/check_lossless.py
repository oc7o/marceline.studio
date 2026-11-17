#!/usr/bin/env python3
"""Prove that condensing the nested CLAUDE.md files deleted nothing (issue #79).

For every non-blank line of the nested files as they stood on `BEFORE` (the commit this branch was
cut from, so it covers everything main added after the first condensation), look for that exact line
in the union of today's CLAUDE.md files and `docs/`. A line that is not found must be listed in
`REWRITTEN` with the reason it was rewritten instead of moved.

    python3 docs/conventions/check_lossless.py          # from the repository root

Exit code 0 means nothing was lost.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

BEFORE = "536d0c2"  # origin/main as this branch left it: the text the condensation must preserve

SOURCES = [
    "backend/matching/CLAUDE.md",
    "backend/crawler/CLAUDE.md",
    "backend/marceline/CLAUDE.md",
    "frontend/CLAUDE.md",
    "frontend/src/app/CLAUDE.md",
]

# Lines deliberately rewritten rather than moved: each was wrong when it was written down, and a
# corrected line is worth more than a verbatim one. Keyed by a distinctive fragment.
REWRITTEN = {
    "m2_report --creator NAME --max-distance 31": (
        "stale: the CLI default is STRICT_MAX_DISTANCE = 36 (matching/references.py). "
        "Corrected to 36 in docs/conventions/matching.md."
    ),
    "`exact_duration_single_thumb`) with": (
        "incomplete: matching/triage.py has a fifth source, metadata_candidate (ADR 0040 "
        "amendment). Added in docs/conventions/matching.md."
    ),
    "**Performance budget (ADR 0028, issue #82).**": (
        "wrong issue number: #82 is the media URL breaker (ADR 0041). Dropped in "
        "docs/conventions/customer-ui.md."
    ),
}


def haystack(root: pathlib.Path) -> str:
    parts = []
    for path in sorted(root.glob("docs/**/*.md")):
        parts.append(path.read_text())
    for name in SOURCES + [
        "CLAUDE.md",
        "backend/CLAUDE.md",
        "backend/marceline/capture/CLAUDE.md",
        "frontend/customer/CLAUDE.md",
    ]:
        path = root / name
        if path.exists():
            parts.append(path.read_text())
    return "\n".join(parts)


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[2]
    text = haystack(root)
    missing: list[tuple[str, str]] = []
    excused: list[tuple[str, str]] = []
    checked = 0
    for name in SOURCES:
        old = subprocess.run(
            ["git", "show", f"{BEFORE}:{name}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for line in old.splitlines():
            if not line.strip():
                continue
            checked += 1
            if line in text:
                continue
            reason = next((why for frag, why in REWRITTEN.items() if frag in line), None)
            if reason:
                excused.append((f"{name}: {line.strip()}", reason))
            else:
                missing.append((name, line))

    print(f"checked {checked} non-blank lines from {len(SOURCES)} files")
    print(f"deliberately rewritten: {len(excused)}")
    for where, why in excused:
        print(f"  - {where}\n      {why}")
    print(f"missing: {len(missing)}")
    for name, line in missing[:20]:
        print(f"  ! {name}: {line}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
