"""Tests for the two-branch diff primitives in `git_ops`."""

from __future__ import annotations

import subprocess
from pathlib import Path

from adw.nodes import git_ops


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_branch_exists(target_repo: Path) -> None:
    assert git_ops.branch_exists(target_repo, "main")
    assert not git_ops.branch_exists(target_repo, "nope")
    assert not git_ops.branch_exists(target_repo, "")


def test_branch_diff(target_repo: Path) -> None:
    _run(target_repo, "checkout", "-b", "feat")
    (target_repo / "app.py").write_text("def hello():\n    return 'howdy'\n")
    _run(target_repo, "commit", "-am", "change greeting")
    _run(target_repo, "checkout", "main")

    diff = git_ops.branch_diff(target_repo, "main", "feat")
    assert "diff --git" in diff
    assert "app.py" in diff
    assert "howdy" in diff

    assert git_ops.branch_diff(target_repo, "main", "main") == ""
