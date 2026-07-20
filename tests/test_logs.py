"""Tests for `adw logs <run-id>`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from adw.adapters.base import AgentResult, TokenUsage
from adw.cli import app
from adw.state.run_state import RunState, create_run_dir, load_state, save_state

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
    state.total_cost_usd = 1.23  # legacy run: cost only, no token fields
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
    # A routed step; the artifact above has no route_reason (the no-reason path).
    routed = {
        "role": "build",
        "backend": "claude-code",
        "model": "haiku",
        "cost_usd": 0.1,
        "output": "built",
        "ok": True,
        "route_reason": "downshift: claude 5h at 96% -> haiku",
    }
    (agent_dir / "02-build.json").write_text(json.dumps(routed))
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
    assert "route=downshift:" in result.output
    assert "routing: 1 step(s) usage-capped, 0 step(s) escalated" in result.output
    # the artifact without a route_reason still renders, without a route note
    assert "01-plan  role=plan model=sonnet cost=$1.23\n" in result.output
    # legacy artifacts carry no tokens: no per-line note, no totals footer
    assert "tokens=" not in result.output
    assert "total tokens" not in result.output


def test_logs_prints_tokens_when_reported(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path, "r1")
    state = load_state(run_dir)
    state.add_usage(
        "build",
        AgentResult(
            ok=True,
            output="",
            session_id=None,
            exit_code=0,
            duration_s=0.0,
            tokens=TokenUsage(
                input_tokens=12_000,
                output_tokens=400,
                cache_read_tokens=1_000,
                cache_write_tokens=50,
            ),
            model="sonnet",
        ),
    )
    save_state(state, run_dir)
    artifact = {
        "role": "build",
        "backend": "claude-code",
        "model": "sonnet",
        "cost_usd": 0.5,
        "output": "built",
        "ok": True,
        "tokens": {
            "input_tokens": 12_000,
            "output_tokens": 400,
            "cache_read_tokens": 1_000,
            "cache_write_tokens": 50,
        },
    }
    (run_dir / "agent" / "03-build.json").write_text(json.dumps(artifact))

    result = runner.invoke(app, ["logs", "r1", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "03-build  role=build model=sonnet cost=$0.50  tokens=12.4k\n" in result.output
    assert "total tokens: 12.0k in / 400 out (cache read 1.0k, write 50)" in result.output
    # a single model: no per-model breakdown lines
    assert "  sonnet: " not in result.output


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
