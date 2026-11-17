"""Whether a path lies inside the source checkout, as a container may see it too (issue #59).

Two callers refuse to write sensitive bytes anywhere near the source tree: the upload spool
(`marceline.uploads.spool`, ADR 0044) and the rights proof vault (`marceline.rights.vault`,
ADR 0034). Both used to test the repository root — the app's code directory's own parent — as an
*ancestor* of the candidate path. That is right in a normal checkout, where the code directory is
`<checkout>/backend` and its parent is the checkout itself. It is wrong in a container, where
`backend/` is bind-mounted at `/app`: that parent is the filesystem root, and an ancestor test
against `/` calls every absolute path "inside the repository" — which is how the spool guard
refused a real volume on the development VPS (trial, 2026-09-18) although the path was exactly the
intended one, and the identical bug is in the vault.

`inside_checkout` fixes this once, for both: the checkout root is only consulted when it is a real
directory above the code (not the filesystem root itself), and the code directory itself is always
consulted, so a spool or vault placed inside the real checkout is still refused.

Pure and Django-free — no settings read, no import of `marceline`, `accounts` or any other project
module — so it can be imported from `core` by every caller without widening any import-linter
contract.
"""

from __future__ import annotations

from pathlib import Path


def inside_checkout(resolved: Path, *, code_dir: Path, checkout_root: Path) -> bool:
    """Whether `resolved` (already `.resolve()`d) lies inside the code directory, or inside the
    checkout root when that root is a real directory above the code rather than the filesystem
    root itself.

    `code_dir` is the app's own code directory, resolved (`settings.BASE_DIR`, e.g.
    `<checkout>/backend` or `/app` in a container). `checkout_root` is normally its parent — the
    checkout in a normal layout, but the filesystem root in a container, where it must not be
    treated as an ancestor of everything.
    """
    directories = [code_dir]
    if checkout_root != Path(checkout_root.anchor):
        directories.append(checkout_root)
    return any(resolved == directory or directory in resolved.parents for directory in directories)
