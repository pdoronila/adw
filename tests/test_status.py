"""Tests for `adw status --json` listing output."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from adw.cli import app
from adw.state.run_state import RunState, create_run_dir, save_state

runner = CliRunner()

EXPECTED_KEYS = {"run_id", "workflow", "status", "total_cost_usd", "outcome_detail"}


def _seed_run(repo: Path, run_id: str) -> None:
    run_dir = create_run_dir(repo, run_id)
    state = RunState(run_id=run_id, workflow="feature", task="t", repo=str(repo))
    state.add_cost(0.25)
    state.outcome_detail = "did the thing"
    save_state(state, run_dir)


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
