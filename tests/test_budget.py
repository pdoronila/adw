"""Budget guardrail: runs pause when cost exceeds limits.max_cost_usd; resume decides."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from adw.adapters.base import AgentAdapter, AgentInvocation
from adw.adapters.mock import MockAdapter, ScriptedTurn
from adw.cli import app
from adw.config import AdwConfig
from adw.nodes.agent_node import AgentRunner
from adw.state.run_state import RunState, create_run_dir, load_state, runs_root, save_state
from adw.workflows.base import WorkflowContext
from adw.workflows.feature import FeatureWorkflow


def make_config(max_cost_usd: float = 0.5) -> AdwConfig:
    return AdwConfig.model_validate(
        {
            "gates": {"marker": {"command": "test -f marker.txt", "timeout": 10}},
            "workflow": {"max_fix_iterations": 1, "gate_order": ["marker"]},
            "limits": {"max_cost_usd": max_cost_usd},
        }
    )


def marker(repo: Path):
    def _do(inv: AgentInvocation) -> None:
        (repo / "marker.txt").write_text("done\n")

    return _do


def build_ctx(
    repo: Path, mocks: dict[str, AgentAdapter], *, decision=None, max_cost_usd: float = 0.5
) -> WorkflowContext:
    run_dir = runs_root(repo) / "test-run"
    if (run_dir / "state.json").is_file():
        state = load_state(run_dir)
    else:
        run_dir = create_run_dir(repo, "test-run")
        state = RunState(run_id="test-run", workflow="feature", task="add marker", repo=str(repo))
        save_state(state, run_dir)
    config = make_config(max_cost_usd)
    return WorkflowContext(
        repo_dir=repo,
        run_dir=run_dir,
        config=config,
        state=state,
        task="add marker",
        agents=AgentRunner(
            config,
            run_dir,
            adapter_factory=lambda role, backend: mocks.setdefault(role, MockAdapter()),
            workflow="feature",
        ),
        mode="async",
        decision=decision,
    )


def test_cost_exactly_at_limit_does_not_pause(target_repo: Path) -> None:
    mocks: dict[str, AgentAdapter] = {
        "scout": MockAdapter([ScriptedTurn(output="s", cost_usd=0.5)]),
        "plan": MockAdapter([ScriptedTurn(output="# Plan", cost_usd=0.0)]),
    }
    out = FeatureWorkflow().run(build_ctx(target_repo, mocks))
    assert out.status == "paused"
    state = load_state(runs_root(target_repo) / "test-run")
    assert state.pending_gate == "plan"  # the plan gate, not the budget gate


def test_over_budget_pauses_with_budget_gate(target_repo: Path) -> None:
    scout = MockAdapter([ScriptedTurn(output="s", cost_usd=0.6)])
    plan = MockAdapter([ScriptedTurn(output="# Plan")])
    mocks: dict[str, AgentAdapter] = {"scout": scout, "plan": plan}

    out = FeatureWorkflow().run(build_ctx(target_repo, mocks))
    assert out.status == "paused"
    state = load_state(runs_root(target_repo) / "test-run")
    assert state.status == "paused"
    assert state.pending_gate == "budget"
    assert state.step("scout").status == "ok"  # the paid step is complete, not re-run
    assert len(plan.invocations) == 0


def test_resume_approve_waives_budget_without_recharge(target_repo: Path) -> None:
    scout = MockAdapter([ScriptedTurn(output="s", cost_usd=0.6)])
    plan = MockAdapter([ScriptedTurn(output="# Plan", cost_usd=0.4)])
    mocks: dict[str, AgentAdapter] = {"scout": scout, "plan": plan}
    assert FeatureWorkflow().run(build_ctx(target_repo, mocks)).status == "paused"

    # approve lifts the budget for the rest of the run: plan runs (still over
    # budget) with no second budget pause; the run reaches the plan gate.
    out = FeatureWorkflow().run(build_ctx(target_repo, mocks, decision="approve"))
    assert out.status == "paused"
    state = load_state(runs_root(target_repo) / "test-run")
    assert state.pending_gate == "plan"
    assert state.budget_waived is True
    assert len(scout.invocations) == 1  # not re-invoked, not re-charged
    assert len(plan.invocations) == 1


def test_resume_reject_stops_without_spending(target_repo: Path) -> None:
    scout = MockAdapter([ScriptedTurn(output="s", cost_usd=0.6)])
    plan = MockAdapter([ScriptedTurn(output="# Plan")])
    mocks: dict[str, AgentAdapter] = {"scout": scout, "plan": plan}
    assert FeatureWorkflow().run(build_ctx(target_repo, mocks)).status == "paused"

    out = FeatureWorkflow().run(build_ctx(target_repo, mocks, decision="reject"))
    assert out.status == "rejected"
    state = load_state(runs_root(target_repo) / "test-run")
    assert state.status == "rejected"
    assert len(plan.invocations) == 0  # a reject never spends more money


def test_resume_edit_on_budget_gate_errors(tmp_path: Path) -> None:
    run_dir = create_run_dir(tmp_path, "r1")
    state = RunState(
        run_id="r1",
        workflow="feature",
        task="t",
        repo=str(tmp_path),
        status="paused",
        pending_gate="budget",
    )
    save_state(state, run_dir)

    result = CliRunner().invoke(app, ["resume", "r1", "--edit", "--repo", str(tmp_path)])
    assert result.exit_code == 2
    assert "no artifact" in result.output
