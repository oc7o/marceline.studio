"""The check runner holds no secret (docs/runbooks/check-runner.md, issue #102).

`infra/check-runner/sync.sh` copies the whole checkout to another host, so the
exclude list has to name `.env` or the main checkout's secrets would travel with every
`make remote-check`. The runbook's promise is only as good as this list.
"""

from pathlib import Path

EXCLUDES = Path(__file__).resolve().parents[3] / "infra" / "check-runner" / "excludes.txt"


def _patterns() -> set[str]:
    return {line.strip() for line in EXCLUDES.read_text().splitlines() if line.strip()}


def test_the_environment_file_never_leaves_this_machine() -> None:
    assert ".env" in _patterns()


def test_the_example_file_still_travels() -> None:
    # `.env.example` is read by tests on the runner; a glob like `.env*` would silently drop it.
    assert not any(p.startswith(".env") and p != ".env" for p in _patterns())
