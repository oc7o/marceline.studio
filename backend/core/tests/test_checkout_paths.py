"""The shared "is this inside the source checkout" rule (issue #59), pure and Django-free.

Two callers (`marceline.uploads.spool`, `marceline.rights.vault`) apply this to refuse a spool or
vault placed anywhere near the code. Both hit the same false positive in a container, where the
code directory is bind-mounted at the container root: this module is the one place the rule is
tested against both layouts.
"""

from __future__ import annotations

from pathlib import Path

from core.checkout_paths import inside_checkout


def test_the_code_directory_itself_is_always_refused() -> None:
    code_dir = Path("/app")
    assert inside_checkout(code_dir, code_dir=code_dir, checkout_root=Path("/"))


def test_a_path_under_the_code_directory_is_refused() -> None:
    code_dir = Path("/app")
    candidate = code_dir / "spool"
    assert inside_checkout(candidate, code_dir=code_dir, checkout_root=Path("/"))


def test_the_container_layout_does_not_call_every_absolute_path_inside_the_repository() -> None:
    """`backend/` bind-mounted at `/app`: its parent is the filesystem root, which must not be
    consulted as an ancestor of everything (the bug the trial found in both callers)."""
    code_dir = Path("/app")
    checkout_root = code_dir.parent
    assert checkout_root == Path("/")
    candidate = Path("/var/lib/marceline/upload-spool")
    assert not inside_checkout(candidate, code_dir=code_dir, checkout_root=checkout_root)


def test_the_checkout_layout_still_refuses_a_path_inside_the_real_repository() -> None:
    """A normal checkout: the code directory's parent is a real directory above it, and a spool
    or vault placed anywhere under that parent is still refused."""
    checkout_root = Path("/home/operator/marceline")
    code_dir = checkout_root / "backend"
    candidate = checkout_root / "frontend" / "dist" / "vault"
    assert inside_checkout(candidate, code_dir=code_dir, checkout_root=checkout_root)


def test_the_checkout_layout_allows_a_path_genuinely_outside_it() -> None:
    checkout_root = Path("/home/operator/marceline")
    code_dir = checkout_root / "backend"
    candidate = Path("/var/lib/marceline/upload-spool")
    assert not inside_checkout(candidate, code_dir=code_dir, checkout_root=checkout_root)
