"""Tests for `adw land` (integrate a shipped run's branch after the fact)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from adw.cli import app
from adw.nodes import git_ops
from adw.state.run_state import (
    RunState,
    RunStatus,
    create_run_dir,
    load_state,
    runs_root,
    save_state,
)

runner = CliRunner()


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _make_branches(repo: Path, *, conflict: bool = False) -> None:
    """A `work` branch with one commit, then `main` advanced by another commit."""
    _run(repo, "checkout", "-b", "work")
    (repo / "app.py").write_text("def hello():\n    return 'work'\n")
    _run(repo, "commit", "-am", "work change")
    _run(repo, "checkout", "main")
    if conflict:
        (repo / "app.py").write_text("def hello():\n    return 'main'\n")
        _run(repo, "commit", "-am", "main change")
    else:
        (repo / "other.py").write_text("x = 1\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-m", "main advance")


def _seed_run(repo: Path, target_repo: Path, *, status: RunStatus = "shipped") -> str:
    """Seed a run dir under `repo` whose state points at `target_repo`."""
    run_id = "r1"
    run_dir = create_run_dir(repo, run_id)
    state = RunState(run_id=run_id, workflow="feature", task="t", repo=str(target_repo))
    state.base_branch = "main"
    state.work_branch = "work"
    state.status = status
    save_state(state, run_dir)
    return run_id


def test_land_cli_success(tmp_path: Path, target_repo: Path) -> None:
    _make_branches(target_repo)
    run_id = _seed_run(tmp_path, target_repo)

    result = runner.invoke(app, ["land", run_id, "--repo", str(tmp_path), "--no-push"])

    assert result.exit_code == 0, result.output
    assert "landed on main" in result.output
    assert "work change" in _out(target_repo, "log", "main", "--oneline")
    assert not git_ops.branch_exists(target_repo, "work")
    state = load_state(runs_root(tmp_path) / run_id)
    assert "landed on main" in state.outcome_detail


def test_land_cli_keep_branch(tmp_path: Path, target_repo: Path) -> None:
    _make_branches(target_repo)
    run_id = _seed_run(tmp_path, target_repo)

    result = runner.invoke(
        app, ["land", run_id, "--repo", str(tmp_path), "--no-push", "--keep-branch"]
    )

    assert result.exit_code == 0, result.output
    assert "work change" in _out(target_repo, "log", "main", "--oneline")
    assert git_ops.branch_exists(target_repo, "work")


def test_land_cli_requires_shipped(tmp_path: Path, target_repo: Path) -> None:
    run_id = _seed_run(tmp_path, target_repo, status="running")

    result = runner.invoke(app, ["land", run_id, "--repo", str(tmp_path)])

    assert result.exit_code == 1
    assert "not shipped" in result.output


def test_land_cli_conflict(tmp_path: Path, target_repo: Path) -> None:
    _make_branches(target_repo, conflict=True)
    run_id = _seed_run(tmp_path, target_repo)

    result = runner.invoke(app, ["land", run_id, "--repo", str(tmp_path), "--no-push"])

    assert result.exit_code == 1
    assert "manual land needed" in result.output
    assert git_ops.branch_exists(target_repo, "work")
