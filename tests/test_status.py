"""Tests for `adw status --json` listing output and `--diff`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from adw.cli import app
from adw.state.run_state import RunState, create_run_dir, save_state

runner = CliRunner()

EXPECTED_KEYS = {"run_id", "workflow", "status", "total_cost_usd", "outcome_detail"}

CHANGED_LINE = "return 'howdy'"


def _seed_run(repo: Path, run_id: str, workflow: str = "feature", cost: float = 0.25) -> None:
    run_dir = create_run_dir(repo, run_id)
    state = RunState(run_id=run_id, workflow=workflow, task="t", repo=str(repo))
    state.add_cost(cost)
    state.outcome_detail = "did the thing"
    save_state(state, run_dir)


def _seed_run_with_branches(
    repo: Path,
    run_id: str,
    target_repo: Path,
    *,
    work_branch: str = "feat",
    create_branch: bool = True,
) -> None:
    """Seed a run whose state points at `target_repo` with base/work branches."""
    run_dir = create_run_dir(repo, run_id)
    state = RunState(run_id=run_id, workflow="feature", task="t", repo=str(target_repo))
    state.base_branch = "main"
    state.work_branch = work_branch
    save_state(state, run_dir)
    if create_branch:
        subprocess.run(
            ["git", "checkout", "-b", work_branch], cwd=target_repo, check=True, capture_output=True
        )
        (target_repo / "app.py").write_text(f"def hello():\n    {CHANGED_LINE}\n")
        subprocess.run(
            ["git", "commit", "-am", "change greeting"],
            cwd=target_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "main"], cwd=target_repo, check=True, capture_output=True
        )


def test_status_json_lists_runs(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    _seed_run(tmp_path, "r2")

    result = runner.invoke(app, ["status", "--json", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    assert isinstance(data, list)
    assert [entry["run_id"] for entry in data] == ["r1", "r2"]
    for entry in data:
        assert set(entry) == EXPECTED_KEYS
    assert data[0]["total_cost_usd"] == 0.25


def test_status_json_empty_is_empty_array(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "--json", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_status_diff_prints_patch(tmp_path: Path, target_repo: Path) -> None:
    _seed_run_with_branches(tmp_path, "r1", target_repo)

    result = runner.invoke(app, ["status", "r1", "--diff", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "diff --git" in result.output
    assert CHANGED_LINE in result.output
    # the JSON state path was skipped
    assert "outcome_detail" not in result.output


def test_status_diff_missing_branch(tmp_path: Path, target_repo: Path) -> None:
    _seed_run_with_branches(tmp_path, "r1", target_repo, create_branch=False)

    result = runner.invoke(app, ["status", "r1", "--diff", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "feat" in result.output


def test_status_diff_no_branch_yet(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")

    result = runner.invoke(app, ["status", "r1", "--diff", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "no work branch" in result.output


def test_status_diff_requires_run_id(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "--diff", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "requires a run id" in result.output


def test_status_costs_plain(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1", workflow="feature", cost=0.25)
    _seed_run(tmp_path, "r2", workflow="feature", cost=0.50)
    _seed_run(tmp_path, "r3", workflow="bug", cost=1.00)

    result = runner.invoke(app, ["status", "--costs", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any("bug" in line and "1 runs" in line and "$    1.00" in line for line in lines)
    assert any("feature" in line and "2 runs" in line and "$    0.75" in line for line in lines)
    assert "total" in lines[-1] and "3 runs" in lines[-1] and "$    1.75" in lines[-1]


def test_status_costs_json(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1", workflow="feature", cost=0.25)
    _seed_run(tmp_path, "r2", workflow="bug", cost=1.00)

    result = runner.invoke(app, ["status", "--costs", "--json", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["runs"] == 2
    assert data["total_cost_usd"] == 1.25
    assert data["workflows"]["feature"] == {"runs": 1, "total_cost_usd": 0.25}
    assert data["workflows"]["bug"] == {"runs": 1, "total_cost_usd": 1.00}


def test_status_costs_rejects_run_id(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    result = runner.invoke(app, ["status", "r1", "--costs", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "cannot be combined" in result.output
