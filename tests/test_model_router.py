"""Tests for the per-role model router: pick_model logic + AgentRunner wiring."""

from __future__ import annotations

import json
from pathlib import Path

from adw.adapters.mock import MockAdapter, ScriptedTurn
from adw.config import AdwConfig
from adw.limits import SessionLimit
from adw.model_router import pick_model
from adw.nodes.agent_node import AgentRunner
from adw.state.run_state import RunState, create_run_dir, save_state
from adw.workflows import steps
from adw.workflows.base import WorkflowContext


def make_config(
    model: str | None = "opus",
    backend: str = "claude-code",
    enabled: bool = True,
    **router: object,
) -> AdwConfig:
    return AdwConfig.model_validate(
        {
            "agents": {"roles": {"build": {"backend": backend, "model": model}}},
            "model_router": {"enabled": enabled, **router},
        }
    )


def make_state(failures: dict[str, int] | None = None) -> RunState:
    state = RunState(run_id="r", workflow="feature", task="t", repo=".")
    if failures:
        state.role_failures = failures
    return state


def claude_at(pct: float, label: str = "5h") -> SessionLimit:
    return SessionLimit(backend="claude", label=label, used_percent=pct)


def _pick(config: AdwConfig, state: RunState, limits: list[SessionLimit]) -> tuple[str | None, str]:
    role_agent = config.resolve_role("build")
    return pick_model("build", role_agent, None, state, config, limits_fn=lambda: limits)


def test_disabled_is_unrouted_and_never_probes() -> None:
    config = make_config(enabled=False)
    calls = {"n": 0}

    def probe() -> list[SessionLimit]:
        calls["n"] += 1
        return [claude_at(99.0)]

    result = pick_model("build", config.resolve_role("build"), None, make_state(), config, probe)
    assert result == ("opus", "unrouted")
    assert calls["n"] == 0


def test_passthrough_cases() -> None:
    # backend without a ladder
    config = make_config(backend="opencode", model="anthropic/claude-opus-4")
    assert _pick(config, make_state(), [claude_at(99.0)]) == (
        "anthropic/claude-opus-4",
        "unrouted",
    )
    # model not on the ladder
    config = make_config(model="fable")
    assert _pick(config, make_state(), [claude_at(99.0)]) == ("fable", "unrouted")
    # model is None (backend default)
    config = make_config(model=None)
    assert _pick(config, make_state(), [claude_at(99.0)]) == (None, "unrouted")


def test_warn_threshold_downshifts_one_rung() -> None:
    model, reason = _pick(make_config(), make_state(), [claude_at(85.0)])
    assert model == "sonnet"
    assert reason.startswith("downshift: claude 5h at 85%")


def test_critical_threshold_jumps_to_cheapest() -> None:
    model, reason = _pick(make_config(), make_state(), [claude_at(96.0)])
    assert model == "haiku"
    assert reason.startswith("downshift: claude 5h at 96%")


def test_backend_name_mapping() -> None:
    # a codex limit must not downshift a claude-code role...
    codex = SessionLimit(backend="codex", label="weekly", used_percent=99.0)
    assert _pick(make_config(), make_state(), [codex]) == ("opus", "hold: opus at target tier")
    # ...but a claude limit must
    model, _ = _pick(make_config(), make_state(), [codex, claude_at(96.0)])
    assert model == "haiku"


def test_peak_window_across_windows() -> None:
    limits = [claude_at(10.0, "5h"), claude_at(96.0, "weekly")]
    model, reason = _pick(make_config(), make_state(), limits)
    assert model == "haiku"
    assert reason.startswith("downshift: claude weekly at 96%")


def test_upshift_on_failures() -> None:
    config = make_config(model="sonnet")
    model, reason = _pick(config, make_state({"build": 2}), [])
    assert model == "opus"
    assert reason == "upshift: build failed gates 2x -> opus"
    # 4 failures from haiku -> two rungs smarter
    config = make_config(model="haiku")
    model, _ = _pick(config, make_state({"build": 4}), [])
    assert model == "opus"


def test_clamped_at_ladder_ends() -> None:
    # already cheapest + critical usage: stays haiku
    model, _ = _pick(make_config(model="haiku"), make_state(), [claude_at(96.0)])
    assert model == "haiku"
    # already best + failures: stays at index 0
    model, reason = _pick(make_config(), make_state({"build": 6}), [])
    assert model == "opus"
    assert reason == "hold: opus at target tier"


def test_usage_pressure_suppresses_upshift() -> None:
    config = make_config(model="sonnet")
    model, reason = _pick(config, make_state({"build": 2}), [claude_at(85.0)])
    assert model == "haiku"  # the downshift wins, not the upshift to opus
    assert "downshift" in reason
    assert "upshift suppressed" in reason


def test_probe_failure_holds_at_target_tier() -> None:
    config = make_config()

    def boom() -> list[SessionLimit]:
        raise RuntimeError("probe down")

    role_agent = config.resolve_role("build")
    result = pick_model("build", role_agent, None, make_state(), config, boom)
    assert result == ("opus", "hold: opus at target tier")
    assert _pick(config, make_state(), []) == ("opus", "hold: opus at target tier")


def test_runner_routes_model_and_persists_reason(tmp_path: Path) -> None:
    config = make_config()
    run_dir = create_run_dir(tmp_path, "r1")
    mock = MockAdapter([ScriptedTurn(output="done")])
    runner = AgentRunner(
        config,
        run_dir,
        adapter_factory=lambda r, b: mock,
        state=make_state(),
        limits_fn=lambda: [claude_at(96.0)],
    )
    runner.run("build", "go", cwd=tmp_path, step_name="build")

    assert mock.invocations[0].model == "haiku"
    artifact = json.loads((run_dir / "agent" / "01-build.json").read_text())
    assert artifact["model"] == "haiku"
    assert artifact["route_reason"].startswith("downshift")


def test_runner_disabled_is_a_noop(tmp_path: Path) -> None:
    config = make_config(enabled=False)
    run_dir = create_run_dir(tmp_path, "r1")
    mock = MockAdapter([ScriptedTurn(output="done")])
    runner = AgentRunner(
        config,
        run_dir,
        adapter_factory=lambda r, b: mock,
        state=make_state(),
        limits_fn=lambda: [claude_at(96.0)],
    )
    runner.run("build", "go", cwd=tmp_path, step_name="build")

    assert mock.invocations[0].model == "opus"
    artifact = json.loads((run_dir / "agent" / "01-build.json").read_text())
    assert artifact["model"] == "opus"
    assert "route_reason" not in artifact


def _gate_ctx(repo: Path, enabled: bool) -> WorkflowContext:
    """A ctx whose marker gate fails once; the fix turn repairs it."""
    config = AdwConfig.model_validate(
        {
            "gates": {"marker": {"command": "test -f marker.txt", "timeout": 10}},
            "workflow": {"max_fix_iterations": 3, "gate_order": ["marker"]},
            "agents": {"roles": {"build": {"backend": "claude-code", "model": "opus"}}},
            "model_router": {"enabled": enabled},
        }
    )
    run_dir = create_run_dir(repo, "test-run")
    state = RunState(run_id="test-run", workflow="feature", task="t", repo=str(repo))
    save_state(state, run_dir)
    mock = MockAdapter(
        [
            ScriptedTurn(
                output="fixed",
                on_invoke=lambda inv: (repo / "marker.txt").write_text("fixed\n"),
            )
        ]
    )
    return WorkflowContext(
        repo_dir=repo,
        run_dir=run_dir,
        config=config,
        state=state,
        task="t",
        agents=AgentRunner(
            config, run_dir, adapter_factory=lambda r, b: mock, state=state, limits_fn=lambda: []
        ),
        assume_yes=True,
    )


def test_gate_loop_counts_failures_when_enabled(tmp_path: Path) -> None:
    ctx = _gate_ctx(tmp_path, enabled=True)
    outcome = steps.gate_loop(ctx)
    assert outcome is None  # gates passed after one fix round
    assert ctx.state.role_failures == {"build": 1}


def test_gate_loop_leaves_counter_untouched_when_disabled(tmp_path: Path) -> None:
    ctx = _gate_ctx(tmp_path, enabled=False)
    outcome = steps.gate_loop(ctx)
    assert outcome is None
    assert ctx.state.role_failures == {}
