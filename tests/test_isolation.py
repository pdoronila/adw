"""Worktree isolation, parallel queue, and racing — via the CLI + fake agent."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adw.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _adw_yaml(fake: Path, isolation: str) -> str:
    return f"""
gates:
  marker: {{command: "test -f marker.txt", timeout: 10}}
agents:
  default: {{backend: claude-code, model: sonnet}}
workflow: {{max_fix_iterations: 2, gate_order: [marker]}}
isolation: {{type: {isolation}}}
backends:
  claude-code: {{binary: "{fake}"}}
"""


@pytest.fixture()
def repo(target_repo: Path) -> Path:
    fake = FIXTURES / "fake_agent.py"
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (target_repo / "adw.yaml").write_text(_adw_yaml(fake, "worktree"))
    subprocess.run(["git", "add", "-A"], cwd=target_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cfg"], cwd=target_repo, check=True, capture_output=True)
    return target_repo


def _branches(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"], cwd=repo, capture_output=True, text=True
    ).stdout
    return out.split()


def test_worktree_isolation_ships_and_cleans_up(repo: Path) -> None:
    result = runner.invoke(
        app,
        ["run", "feature", "add hello", "--repo", str(repo), "-y"],
        env={**os.environ, "ADW_ASSUME_YES": "1"},
    )
    assert result.exit_code == 0, result.output
    assert "shipped" in result.output

    # the fix landed on an adw/ branch (committed in the worktree)…
    adw_branches = [b for b in _branches(repo) if b.startswith("adw/")]
    assert len(adw_branches) == 1
    log = subprocess.run(
        ["git", "log", adw_branches[0], "--oneline"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "add hello" in log
    # …the main working tree stayed on main and untouched…
    assert subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    ).stdout.strip() == "main"
    assert not (repo / "feature.txt").exists()  # only in the worktree's branch
    # …and the worktree was cleaned up after shipping.
    assert not (repo / ".adw" / "worktrees").exists() or not list(
        (repo / ".adw" / "worktrees").iterdir()
    )


def test_race_picks_a_winner(repo: Path) -> None:
    result = runner.invoke(
        app,
        ["run", "feature", "add hello", "--repo", str(repo), "--race", "3"],
        env={**os.environ, "ADW_ASSUME_YES": "1"},
    )
    assert result.exit_code == 0, result.output
    assert "winner" in result.output
    # exactly one adw/ branch remains — losers were dropped
    assert len([b for b in _branches(repo) if b.startswith("adw/")]) == 1


def test_parallel_requires_isolation(target_repo: Path) -> None:
    fake = FIXTURES / "fake_agent.py"
    (target_repo / "adw.yaml").write_text(_adw_yaml(fake, "local"))
    result = runner.invoke(
        app, ["queue", "process", "--parallel", "2", "-y", "--repo", str(target_repo)]
    )
    assert result.exit_code == 2
    assert "worktree" in result.output


def test_parallel_queue_processes_all(repo: Path) -> None:
    for i in range(3):
        runner.invoke(
            app, ["ticket", "new", f"task {i}", "--repo", str(repo), "--body", "do it"]
        )
    result = runner.invoke(
        app,
        ["queue", "process", "--all", "--parallel", "2", "-y", "--repo", str(repo)],
        env={**os.environ, "ADW_ASSUME_YES": "1"},
    )
    assert result.exit_code == 0, result.output
    done = list((repo / ".adw" / "tickets" / "done").glob("*.md"))
    assert len(done) == 3
