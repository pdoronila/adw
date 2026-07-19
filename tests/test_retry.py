"""adw retry: reset a failed run's failed step and re-drive it from the checkpoint."""

from __future__ import annotations

from pathlib import Path

from adw.adapters.base import AgentAdapter, AgentInvocation
from adw.adapters.mock import MockAdapter, ScriptedTurn
from adw.config import AdwConfig
from adw.nodes.agent_node import AgentRunner
from adw.state.run_state import RunState, create_run_dir, load_state, runs_root, save_state
from adw.workflows.base import WorkflowContext
from adw.workflows.feature import FeatureWorkflow


def make_config() -> AdwConfig:
    return AdwConfig.model_validate(
        {
            "gates": {"marker": {"command": "test -f marker.txt", "timeout": 10}},
            "workflow": {"max_fix_iterations": 1, "gate_order": ["marker"]},
        }
    )


def marker(repo: Path):
    def _do(inv: AgentInvocation) -> None:
        (repo / "marker.txt").write_text("done\n")

    return _do


def build_ctx(repo: Path, mocks: dict[str, AgentAdapter]) -> WorkflowContext:
    run_dir = runs_root(repo) / "test-run"
    if (run_dir / "state.json").is_file():
        state = load_state(run_dir)
    else:
        run_dir = create_run_dir(repo, "test-run")
        state = RunState(run_id="test-run", workflow="feature", task="add marker", repo=str(repo))
        save_state(state, run_dir)
    return WorkflowContext(
        repo_dir=repo,
        run_dir=run_dir,
        config=make_config(),
        state=state,
        task="add marker",
        agents=AgentRunner(
            make_config(),
            run_dir,
            adapter_factory=lambda role, backend: mocks.setdefault(role, MockAdapter()),
            workflow="feature",
        ),
        assume_yes=True,
        mode="async",
    )


def _apply_retry_reset(state) -> None:
    """Mirrors the reset logic in `adw retry`."""
    state.status = "running"
    state.outcome_detail = ""
    for record in state.steps:
        if record.status == "failed":
            record.status = "pending"


def test_retry_after_build_failure_ships(target_repo: Path) -> None:
    scout = MockAdapter([ScriptedTurn(output="scouted")])
    plan = MockAdapter([ScriptedTurn(output="# Plan", session_id="p")])
    build = MockAdapter([ScriptedTurn(ok=False, output="")])
    review = MockAdapter([ScriptedTurn(output="VERDICT: ship")])
    mocks: dict[str, AgentAdapter] = {
        "scout": scout, "plan": plan, "build": build, "review": review
    }

    # 1) assume_yes auto-approves the plan gate, so the run reaches build directly;
    #    build fails, so the whole run fails.
    ctx = build_ctx(target_repo, mocks)
    outcome = FeatureWorkflow().run(ctx)
    assert outcome.status == "failed"
    state = load_state(ctx.run_dir)
    assert state.status == "failed"
    assert state.step("build").status == "failed"
    assert len(scout.invocations) == 1 and len(plan.invocations) == 1
    assert len(build.invocations) == 1

    # 2) simulate `adw retry`: reload state, reset the failed step, re-drive.
    run_dir = ctx.run_dir
    state = load_state(run_dir)
    assert state.status == "failed"
    _apply_retry_reset(state)
    save_state(state, run_dir)

    mocks["build"] = MockAdapter(
        [ScriptedTurn(output="built", session_id="b", on_invoke=marker(target_repo))]
    )
    retry_ctx = build_ctx(target_repo, mocks)
    outcome = FeatureWorkflow().run(retry_ctx)

    assert outcome.status == "shipped"
    assert retry_ctx.state.status == "shipped"
    # scout and plan were not re-invoked — the skip-guards kicked in.
    assert len(scout.invocations) == 1
    assert len(plan.invocations) == 1
