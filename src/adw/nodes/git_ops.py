"""Deterministic git code nodes."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


# adw's own artifacts (.adw/runs, .adw/tickets) live inside the target repo;
# they must never count as dirt or get swept into a ship commit.
_EXCLUDE_ADW = ":(exclude).adw"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def is_git_repo(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--git-dir", check=False).returncode == 0


def ensure_clean(repo: Path) -> bool:
    return not _git(repo, "status", "--porcelain", "--", ".", _EXCLUDE_ADW).stdout.strip()


def current_branch(repo: Path) -> str:
    # --show-current works even on an unborn branch (no commits yet)
    return _git(repo, "branch", "--show-current").stdout.strip()


def create_branch(repo: Path, name: str) -> None:
    _git(repo, "checkout", "-b", name)


def checkout(repo: Path, name: str) -> None:
    _git(repo, "checkout", name)


def delete_branch(repo: Path, name: str) -> None:
    _git(repo, "branch", "-D", name, check=False)


def diff_summary(repo: Path, base: str) -> str:
    stat = _git(repo, "diff", "--stat", base, "--", ".", _EXCLUDE_ADW, check=False).stdout
    status = _git(repo, "status", "--short", "--", ".", _EXCLUDE_ADW).stdout
    return f"## git diff --stat vs {base}\n{stat}\n## git status --short\n{status}"


def full_diff(repo: Path, base: str) -> str:
    return _git(repo, "diff", base, "--", ".", _EXCLUDE_ADW, check=False).stdout


def stage_all(repo: Path) -> None:
    """Stage everything (except .adw) so new files show up in diffs vs the base."""
    _git(repo, "add", "-A", "--", ".", _EXCLUDE_ADW)


def commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A", "--", ".", _EXCLUDE_ADW)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()


def create_pr(repo: Path, title: str, body: str) -> str:
    proc = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(f"gh pr create failed: {proc.stderr.strip()}")
    return proc.stdout.strip()
