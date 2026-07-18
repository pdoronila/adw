"""Tests for the two-branch diff, rebase, and ff-merge primitives in `git_ops`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from adw.nodes import git_ops


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _sha(repo: Path, ref: str = "HEAD") -> str:
    proc = subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


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


def test_rebase_onto_clean(target_repo: Path) -> None:
    _run(target_repo, "checkout", "-b", "feat")
    (target_repo / "app.py").write_text("def hello():\n    return 'howdy'\n")
    _run(target_repo, "commit", "-am", "change greeting")
    _run(target_repo, "checkout", "main")
    (target_repo / "other.py").write_text("x = 1\n")
    _run(target_repo, "add", "-A")
    _run(target_repo, "commit", "-m", "add other")
    _run(target_repo, "checkout", "feat")

    git_ops.rebase_onto(target_repo, "main")

    _run(target_repo, "merge-base", "--is-ancestor", "main", "feat")  # non-zero would raise
    assert (target_repo / "other.py").is_file()
    assert "howdy" in (target_repo / "app.py").read_text()


def test_rebase_onto_conflict_aborts(target_repo: Path) -> None:
    _run(target_repo, "checkout", "-b", "feat")
    (target_repo / "app.py").write_text("def hello():\n    return 'feat'\n")
    _run(target_repo, "commit", "-am", "feat greeting")
    _run(target_repo, "checkout", "main")
    (target_repo / "app.py").write_text("def hello():\n    return 'main'\n")
    _run(target_repo, "commit", "-am", "main greeting")
    _run(target_repo, "checkout", "feat")
    before = _sha(target_repo, "feat")

    with pytest.raises(git_ops.GitError):
        git_ops.rebase_onto(target_repo, "main")

    assert _sha(target_repo, "feat") == before
    assert git_ops.ensure_clean(target_repo)
    assert not (target_repo / ".git" / "rebase-merge").exists()


def test_merge_ff_only(target_repo: Path) -> None:
    _run(target_repo, "checkout", "-b", "feat")
    (target_repo / "app.py").write_text("def hello():\n    return 'howdy'\n")
    _run(target_repo, "commit", "-am", "change greeting")
    _run(target_repo, "checkout", "main")

    git_ops.merge_ff_only(target_repo, "feat")

    assert _sha(target_repo, "main") == _sha(target_repo, "feat")


def test_merge_ff_only_diverged_raises(target_repo: Path) -> None:
    _run(target_repo, "checkout", "-b", "feat")
    (target_repo / "app.py").write_text("def hello():\n    return 'feat'\n")
    _run(target_repo, "commit", "-am", "feat greeting")
    _run(target_repo, "checkout", "main")
    (target_repo / "other.py").write_text("x = 1\n")
    _run(target_repo, "add", "-A")
    _run(target_repo, "commit", "-m", "diverge")

    with pytest.raises(git_ops.GitError):
        git_ops.merge_ff_only(target_repo, "feat")
