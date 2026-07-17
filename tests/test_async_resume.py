"""Async runs pause at engineer gates; resume continues without re-running agents."""

from __future__ import annotations

from pathlib import Path

import pytest

import adw.workflows.steps as steps
from adw.adapters.base import AgentAdapter, AgentInvocation
from adw.adapters.mock import MockAdapter, ScriptedTurn
from adw.config import AdwConfig
from adw.nodes.agent_node import AgentRunner
from adw.state.run_state import RunState, create_run_dir, load_state, save_state
from adw.workflows.base import WorkflowContext
from adw.workflows.feature import FeatureWorkflow


def make_config() -> AdwConfig:
    return AdwConfig.model_validate(
        {
            "gates": {"marker": {"command": "test -f marker.txt", "timeout": 10}},
            "workflow": {"max_fix_iterations": 1, "gate_order": ["marker"]},
            "notify": {"webhook": "https://example.test/hook"},
        }
    )


def marker(repo: Path):
    def _do(inv: AgentInvocation) -> None:
        (repo / "marker.txt").write_text("done\n")

    return _do


def build_ctx(
    repo: Path, mocks: dict[str, AgentAdapter], *, decision=None
) -> WorkflowContext:
    run_dir = repo / ".adw" / "runs" / "test-run"
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
        mode="async",
        decision=decision,
    )


def test_async_pauses_then_resume_ships(target_repo: Path) -> None:
    scout = MockAdapter([ScriptedTurn(output="scouted")])
    plan = MockAdapter([ScriptedTurn(output="# Plan", session_id="p")])
    build = MockAdapter(
        [ScriptedTurn(output="built", session_id="b", on_invoke=marker(target_repo))]
    )
    review = MockAdapter([ScriptedTurn(output="VERDICT: ship")])
    mocks: dict[str, AgentAdapter] = {
        "scout": scout, "plan": plan, "build": build, "review": review
    }

    # 1) async run pauses at the plan gate — scout + plan ran, build did not
    ctx = build_ctx(target_repo, mocks)
    out = FeatureWorkflow().run(ctx)
    assert out.status == "paused"
    assert load_state(ctx.run_dir).pending_gate == "plan"
    assert len(scout.invocations) == 1 and len(plan.invocations) == 1
    assert len(build.invocations) == 0

    # 2) resume --approve: build+gates+review run, then pause at the final gate.
    #    scout and plan must NOT run again.
    ctx = build_ctx(target_repo, mocks, decision="approve")
    out = FeatureWorkflow().run(ctx)
    assert out.status == "paused"
    assert load_state(ctx.run_dir).pending_gate == "final"
    assert len(scout.invocations) == 1  # not re-invoked
    assert len(plan.invocations) == 1  # not re-invoked
    assert len(build.invocations) == 1
    assert len(review.invocations) == 1

    # 3) resume --approve the final gate: ships. Nothing re-runs.
    ctx = build_ctx(target_repo, mocks, decision="approve")
    out = FeatureWorkflow().run(ctx)
    assert out.status == "shipped"
    assert len(build.invocations) == 1 and len(review.invocations) == 1
    assert load_state(ctx.run_dir).status == "shipped"


def test_async_resume_reject_at_plan(target_repo: Path) -> None:
    mocks: dict[str, AgentAdapter] = {
        "scout": MockAdapter([ScriptedTurn(output="s")]),
        "plan": MockAdapter([ScriptedTurn(output="# Plan")]),
        "build": MockAdapter(),
    }
    ctx = build_ctx(target_repo, mocks)
    assert FeatureWorkflow().run(ctx).status == "paused"

    ctx = build_ctx(target_repo, mocks, decision="reject")
    out = FeatureWorkflow().run(ctx)
    assert out.status == "rejected"
    assert mocks["build"].invocations == []  # type: ignore[attr-defined]


def test_transcripts_numbered_across_resume(target_repo: Path) -> None:
    mocks: dict[str, AgentAdapter] = {
        "scout": MockAdapter([ScriptedTurn(output="s")]),
        "plan": MockAdapter([ScriptedTurn(output="# Plan")]),
        "build": MockAdapter([ScriptedTurn(output="b", on_invoke=marker(target_repo))]),
        "review": MockAdapter([ScriptedTurn(output="VERDICT: ship")]),
    }
    ctx = build_ctx(target_repo, mocks)
    FeatureWorkflow().run(ctx)  # scout, plan -> pause
    ctx = build_ctx(target_repo, mocks, decision="approve")
    FeatureWorkflow().run(ctx)  # build, review -> pause
    names = sorted(p.name for p in (ctx.run_dir / "agent").iterdir())
    # monotonic numbering, no collisions across the resume boundary
    assert names == ["01-scout.json", "02-plan.json", "03-build.json", "04-review.json"]


def test_notify_fires_once_per_pause(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired: list[str] = []
    monkeypatch.setattr(steps, "notify", lambda state, config: fired.append(state.status))
    mocks: dict[str, AgentAdapter] = {
        "scout": MockAdapter([ScriptedTurn(output="s")]),
        "plan": MockAdapter([ScriptedTurn(output="# Plan")]),
        "build": MockAdapter([ScriptedTurn(output="b", on_invoke=marker(target_repo))]),
        "review": MockAdapter([ScriptedTurn(output="VERDICT: ship")]),
    }
    # scout+plan -> pause at plan gate (one notify)
    FeatureWorkflow().run(build_ctx(target_repo, mocks))
    # approve -> build+gates+review -> pause at final gate (one notify, no double-fire)
    FeatureWorkflow().run(build_ctx(target_repo, mocks, decision="approve"))
    # approve -> ships (no notify)
    out = FeatureWorkflow().run(build_ctx(target_repo, mocks, decision="approve"))
    assert out.status == "shipped"
    assert fired == ["awaiting_plan_approval", "awaiting_final_review"]


def test_notify_fires_on_failure(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired: list[str] = []
    monkeypatch.setattr(steps, "notify", lambda state, config: fired.append(state.status))
    # build never writes marker.txt, so the marker gate never passes and fixes exhaust
    mocks: dict[str, AgentAdapter] = {
        "scout": MockAdapter([ScriptedTurn(output="s")]),
        "plan": MockAdapter([ScriptedTurn(output="# Plan")]),
        "build": MockAdapter([ScriptedTurn(output="b"), ScriptedTurn(output="b2")]),
    }
    FeatureWorkflow().run(build_ctx(target_repo, mocks))  # pause at plan
    out = FeatureWorkflow().run(build_ctx(target_repo, mocks, decision="approve"))
    assert out.status == "failed"
    assert fired == ["awaiting_plan_approval", "failed"]
