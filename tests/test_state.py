from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from adw.adapters.base import AgentResult, TokenUsage
from adw.state.run_state import (
    RunState,
    cost_rollup,
    create_run_dir,
    list_runs,
    load_state,
    new_run_id,
    runs_root,
    save_state,
    slugify,
)


def _result(
    cost: float | None = None,
    tokens: TokenUsage | None = None,
    model_tokens: dict[str, TokenUsage] | None = None,
    model: str | None = None,
) -> AgentResult:
    return AgentResult(
        ok=True,
        output="",
        session_id=None,
        exit_code=0,
        duration_s=0.0,
        cost_usd=cost,
        tokens=tokens,
        model_tokens=model_tokens or {},
        model=model,
    )


def test_slugify() -> None:
    assert slugify("Add a --json flag!") == "add-a-json-flag"
    assert slugify("   ") == "task"
    assert len(slugify("x" * 100)) <= 24


def test_run_id_shape() -> None:
    run_id = new_run_id("Fix the thing")
    assert "fix-the-thing" in run_id
    assert len(run_id.split("-", 2)) == 3


def test_state_roundtrip(tmp_path: Path) -> None:
    run_dir = create_run_dir(tmp_path, "r1")
    state = RunState(run_id="r1", workflow="feature", task="do it", repo=str(tmp_path))
    state.start_step("plan")
    state.end_step("plan", "ok", "session s1")
    state.build_session_id = "s1"
    state.add_usage("plan", _result(cost=0.25))
    state.add_usage("plan", _result(cost=None))
    save_state(state, run_dir)
    loaded = load_state(run_dir)
    assert loaded.build_session_id == "s1"
    assert loaded.total_cost_usd == 0.25
    assert loaded.step("plan").status == "ok"
    assert (run_dir / "agent").is_dir() and (run_dir / "gates").is_dir()


def test_save_is_atomic_no_stray_tmp(tmp_path: Path) -> None:
    run_dir = create_run_dir(tmp_path, "r2")
    state = RunState(run_id="r2", workflow="feature", task="t", repo=str(tmp_path))
    for _ in range(3):
        save_state(state, run_dir)
    leftovers = [p for p in run_dir.iterdir() if p.name.startswith(".state-")]
    assert leftovers == []


def test_cancelled_status_and_pid_fields_roundtrip(tmp_path: Path) -> None:
    run_dir = create_run_dir(tmp_path, "rc")
    state = RunState(run_id="rc", workflow="feature", task="t", repo=str(tmp_path))
    state.status = "cancelled"
    state.pid = 123
    state.pgid = 123
    save_state(state, run_dir)
    loaded = load_state(run_dir)
    assert loaded.status == "cancelled"
    assert loaded.pid == 123
    assert loaded.pgid == 123

    # An older state.json without pid/pgid keys still loads (fields default to None).
    payload = json.loads((run_dir / "state.json").read_text())
    del payload["pid"]
    del payload["pgid"]
    (run_dir / "state.json").write_text(json.dumps(payload))
    reloaded = load_state(run_dir)
    assert reloaded.pid is None
    assert reloaded.pgid is None


def _run(workflow: str, cost: float) -> RunState:
    state = RunState(run_id=f"{workflow}-{cost}", workflow=workflow, task="t", repo="")
    state.add_usage("build", _result(cost=cost))
    return state


def test_cost_rollup() -> None:
    rollup = cost_rollup([_run("feature", 0.25), _run("feature", 0.50), _run("bug", 1.00)])
    assert rollup.runs == 3
    assert rollup.total_cost_usd == 1.75
    assert rollup.workflows["feature"].runs == 2
    assert rollup.workflows["feature"].total_cost_usd == 0.75
    assert rollup.workflows["bug"].runs == 1
    assert rollup.workflows["bug"].total_cost_usd == 1.00


def test_add_usage_accumulates_step_run_and_model(tmp_path: Path) -> None:
    state = RunState(run_id="r1", workflow="feature", task="t", repo=str(tmp_path))
    state.add_usage(
        "build",
        _result(
            cost=0.5,
            tokens=TokenUsage(input_tokens=100, output_tokens=20, cache_read_tokens=1000),
            model="sonnet",
        ),
    )
    state.add_usage(
        "review",
        _result(tokens=TokenUsage(input_tokens=10, output_tokens=5), model="haiku"),
    )
    assert state.total_cost_usd == 0.5
    assert state.step("build").tokens == TokenUsage(
        input_tokens=100, output_tokens=20, cache_read_tokens=1000
    )
    assert state.step("review").tokens == TokenUsage(input_tokens=10, output_tokens=5)
    assert state.total_tokens == TokenUsage(
        input_tokens=110, output_tokens=25, cache_read_tokens=1000
    )
    assert state.total_tokens.total == 135  # headline excludes cache traffic
    # fallback attribution: each invocation lands on its runner-stamped model
    assert state.tokens_by_model["sonnet"].total == 120
    assert state.tokens_by_model["haiku"].total == 15


def test_add_usage_prefers_backend_model_breakdown() -> None:
    state = RunState(run_id="r1", workflow="feature", task="t", repo="")
    state.add_usage(
        "build",
        _result(
            tokens=TokenUsage(input_tokens=30, output_tokens=12),
            model_tokens={
                "claude-sonnet-4-5": TokenUsage(input_tokens=25, output_tokens=10),
                "claude-haiku-4-5": TokenUsage(input_tokens=5, output_tokens=2),
            },
            model="sonnet",
        ),
    )
    state.add_usage(
        "fix-1",
        _result(
            tokens=TokenUsage(input_tokens=8, output_tokens=4),
            model_tokens={"claude-sonnet-4-5": TokenUsage(input_tokens=8, output_tokens=4)},
            model="sonnet",
        ),
    )
    # backend-reported per-model usage wins; the stamped alias never appears
    assert "sonnet" not in state.tokens_by_model
    assert state.tokens_by_model["claude-sonnet-4-5"].total == 47
    assert state.tokens_by_model["claude-haiku-4-5"].total == 7
    assert state.total_tokens.total == 54


def test_add_usage_without_tokens_leaves_state_untouched() -> None:
    state = RunState(run_id="r1", workflow="feature", task="t", repo="")
    state.add_usage("build", _result(cost=0.25, tokens=None))
    assert state.total_cost_usd == 0.25
    assert state.step("build").tokens is None
    assert state.total_tokens == TokenUsage()
    assert state.tokens_by_model == {}


def test_add_usage_unknown_model_fallback() -> None:
    state = RunState(run_id="r1", workflow="feature", task="t", repo="")
    state.add_usage("build", _result(tokens=TokenUsage(input_tokens=3, output_tokens=1)))
    assert state.tokens_by_model["unknown"].total == 4


def test_legacy_state_without_token_fields_loads(tmp_path: Path) -> None:
    run_dir = create_run_dir(tmp_path, "old")
    state = RunState(run_id="old", workflow="feature", task="t", repo=str(tmp_path))
    state.start_step("plan")
    save_state(state, run_dir)
    payload = json.loads((run_dir / "state.json").read_text())
    del payload["total_tokens"]
    del payload["tokens_by_model"]
    del payload["steps"][0]["tokens"]
    (run_dir / "state.json").write_text(json.dumps(payload))

    loaded = load_state(run_dir)
    assert loaded.total_tokens == TokenUsage()
    assert loaded.tokens_by_model == {}
    assert loaded.step("plan").tokens is None


def test_cost_rollup_empty() -> None:
    rollup = cost_rollup([])
    assert rollup.runs == 0
    assert rollup.total_cost_usd == 0.0
    assert rollup.workflows == {}


def test_list_runs_skips_junk(tmp_path: Path) -> None:
    run_dir = create_run_dir(tmp_path, "r3")
    save_state(RunState(run_id="r3", workflow="feature", task="t", repo=""), run_dir)
    (runs_root(tmp_path) / "junk").mkdir()
    runs = list_runs(tmp_path)
    assert [r.run_id for r in runs] == ["r3"]


def test_runs_root_defaults_to_user_tier(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    root = runs_root(repo)
    data_home = Path(os.environ["ADW_DATA_HOME"])
    assert root.is_relative_to(data_home)
    assert root.name == "runs"
    run_dir = create_run_dir(repo, "r1")
    save_state(RunState(run_id="r1", workflow="feature", task="t", repo=str(repo)), run_dir)
    assert [r.run_id for r in list_runs(repo)] == ["r1"]
    assert not (repo / ".adw").exists()


def test_runs_root_prefers_existing_project_tier(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".adw" / "runs").mkdir(parents=True)
    assert runs_root(repo) == repo / ".adw" / "runs"
    run_dir = create_run_dir(repo, "r1")
    assert run_dir == repo / ".adw" / "runs" / "r1"
    save_state(RunState(run_id="r1", workflow="feature", task="t", repo=str(repo)), run_dir)
    assert [r.run_id for r in list_runs(repo)] == ["r1"]


def test_runs_root_tier_forced_by_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_repo = tmp_path / "legacy"
    (legacy_repo / ".adw" / "runs").mkdir(parents=True)
    monkeypatch.setenv("ADW_DATA_TIER", "user")
    data_home = Path(os.environ["ADW_DATA_HOME"])
    assert runs_root(legacy_repo).is_relative_to(data_home)

    fresh_repo = tmp_path / "fresh"
    fresh_repo.mkdir()
    monkeypatch.setenv("ADW_DATA_TIER", "project")
    assert runs_root(fresh_repo) == fresh_repo / ".adw" / "runs"


def test_same_basename_repos_do_not_collide(tmp_path: Path) -> None:
    repo_x = tmp_path / "x" / "app"
    repo_y = tmp_path / "y" / "app"
    repo_x.mkdir(parents=True)
    repo_y.mkdir(parents=True)
    for repo, run_id in ((repo_x, "rx"), (repo_y, "ry")):
        run_dir = create_run_dir(repo, run_id)
        save_state(RunState(run_id=run_id, workflow="feature", task="t", repo=str(repo)), run_dir)
    assert [r.run_id for r in list_runs(repo_x)] == ["rx"]
    assert [r.run_id for r in list_runs(repo_y)] == ["ry"]
