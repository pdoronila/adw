"""Tests for `adw logs <run-id>`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from adw.cli import app
from adw.state.run_state import RunState, create_run_dir, save_state

runner = CliRunner()


def _seed_run(repo: Path, run_id: str) -> Path:
    run_dir = create_run_dir(repo, run_id)
    state = RunState(run_id=run_id, workflow="feature", task="add widget", repo=str(repo))
    state.start_step("plan")
    state.end_step("plan", "ok", detail="wrote plan.md")
    state.gate_results.append(
        {
            "attempt": 1,
            "results": [{"name": "lint", "ok": True, "exit_code": 0}],
        }
    )
    state.add_cost(1.23)
    save_state(state, run_dir)

    agent_dir = run_dir / "agent"
    artifact = {
        "role": "plan",
        "backend": "claude-code",
        "model": "sonnet",
        "cost_usd": 1.23,
        "output": "x" * 1000,
        "ok": True,
    }
    (agent_dir / "01-plan.json").write_text(json.dumps(artifact))
    return run_dir


def test_logs_prints_run_detail(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")

    result = runner.invoke(app, ["logs", "r1", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "r1" in result.output
    assert "plan" in result.output
    assert "lint" in result.output
    assert "$1.23" in result.output
    assert str(tmp_path) in result.output


def test_logs_respects_tail_option(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")

    result = runner.invoke(app, ["logs", "r1", "--repo", str(tmp_path), "--tail", "10"])
    assert result.exit_code == 0, result.output
    assert "x" * 1000 not in result.output
    assert "x" * 10 in result.output


def test_logs_missing_run_errors(tmp_path: Path) -> None:
    result = runner.invoke(app, ["logs", "nope", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "no run" in result.output
