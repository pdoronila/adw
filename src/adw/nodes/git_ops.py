"""Deterministic git code nodes."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def is_git_repo(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--git-dir", check=False).returncode == 0


def ensure_adw_ignored(repo: Path) -> None:
    """Register .adw/ in the repo-local .git/info/exclude.

    adw's own artifacts (.adw/runs, .adw/tickets) live inside the target repo;
    this keeps them out of `status`/`diff`/`add` without touching the user's
    tracked .gitignore. Idempotent; no-op outside a git repo.
    """
    proc = _git(repo, "rev-parse", "--git-dir", check=False)
    if proc.returncode != 0:
        return
    git_dir = Path(proc.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    info = git_dir / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    existing = exclude.read_text() if exclude.is_file() else ""
    if ".adw/" in existing.split():
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with exclude.open("a") as handle:
        handle.write(f"{prefix}.adw/\n")


def ensure_clean(repo: Path) -> bool:
    # .adw is excluded via .git/info/exclude, so it never shows here.
    return not _git(repo, "status", "--porcelain").stdout.strip()


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
    stat = _git(repo, "diff", "--stat", base, check=False).stdout
    status = _git(repo, "status", "--short").stdout
    return f"## git diff --stat vs {base}\n{stat}\n## git status --short\n{status}"


def full_diff(repo: Path, base: str) -> str:
    return _git(repo, "diff", base, check=False).stdout


def stage_all(repo: Path) -> None:
    """Stage everything so new files show up in diffs vs the base (.adw is ignored)."""
    _git(repo, "add", "-A")


def commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()


def add_worktree(repo: Path, path: Path, branch: str, base: str) -> None:
    """Create a new worktree at `path` on a fresh `branch` off `base`."""
    _git(repo, "worktree", "add", "-b", branch, str(path), base)


def remove_worktree(repo: Path, path: Path) -> None:
    """Remove a worktree (the branch and its commits remain)."""
    _git(repo, "worktree", "remove", "--force", str(path), check=False)


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
