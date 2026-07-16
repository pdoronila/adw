"""Workflow logic tests: MockAdapters, real git, real (trivial) gates, no CLIs."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from adw.adapters.base import AgentAdapter, AgentInvocation
from adw.adapters.mock import MockAdapter, ScriptedTurn
from adw.config import AdwConfig
from adw.nodes import git_ops
from adw.nodes.agent_node import AgentRunner
from adw.state.run_state import RunState, create_run_dir, save_state
from adw.workflows import steps
from adw.workflows.base import WorkflowContext
from adw.workflows.feature import FeatureWorkflow


def make_config(max_fixes: int = 3, max_reviews: int = 2) -> AdwConfig:
    return AdwConfig.model_validate(
        {
            "gates": {"marker": {"command": "test -f marker.txt", "timeout": 10}},
            "workflow": {
                "max_fix_iterations": max_fixes,
                "max_review_iterations": max_reviews,
                "gate_order": ["marker"],
            },
        }
    )


def make_ctx(
    repo: Path,
    config: AdwConfig,
    mocks: dict[str, AgentAdapter],
    **flags: bool,
) -> WorkflowContext:
    run_id = "test-run"
    run_dir = create_run_dir(repo, run_id)
    state = RunState(run_id=run_id, workflow="feature", task="add a marker", repo=str(repo))
    save_state(state, run_dir)

    def factory(role: str, backend: str) -> AgentAdapter:
        # Unlisted roles (e.g. scout) get a permissive default adapter.
        return mocks.setdefault(role, MockAdapter())

    return WorkflowContext(
        repo_dir=repo,
        run_dir=run_dir,
        config=config,
        state=state,
        task="add a marker",
        agents=AgentRunner(config, run_dir, adapter_factory=factory, workflow="feature"),
        assume_yes=True,
        **flags,
    )


def touch_marker(repo: Path):
    def _do(inv: AgentInvocation) -> None:
        (repo / "marker.txt").write_text("fixed\n")

    return _do


def edit_app(repo: Path):
    def _do(inv: AgentInvocation) -> None:
        (repo / "app.py").write_text("def hello():\n    return 'hello, adw'\n")

    return _do


def test_happy_path_with_one_fix_round(target_repo: Path) -> None:
    """Build leaves gates failing; fix (same session) repairs; run ships."""
    build_mock = MockAdapter(
        [
            ScriptedTurn(output="built", session_id="build-s1", on_invoke=edit_app(target_repo)),
            ScriptedTurn(
                output="fixed", session_id="build-s1", on_invoke=touch_marker(target_repo)
            ),
        ]
    )
    mocks: dict[str, AgentAdapter] = {
        "scout": MockAdapter([ScriptedTurn(output="found app.py", session_id="scout-s")]),
        "plan": MockAdapter([ScriptedTurn(output="# Plan\nAdd marker.", session_id="plan-s")]),
        "build": build_mock,
        "review": MockAdapter([ScriptedTurn(output="VERDICT: ship", session_id="review-s")]),
    }
    ctx = make_ctx(target_repo, make_config(), mocks)
    outcome = FeatureWorkflow().run(ctx)

    assert outcome.status == "shipped"
    # scout ran read-only before plan, and its findings reached the plan prompt
    assert mocks["scout"].invocations[0].read_only  # type: ignore[attr-defined]
    assert "found app.py" in mocks["plan"].invocations[0].prompt  # type: ignore[attr-defined]
    # fix resumed the SAME build session with the gate failure in the prompt
    assert len(build_mock.invocations) == 2
    fix_inv = build_mock.invocations[1]
    assert fix_inv.session_id == "build-s1"
    assert "Gate `marker` failed" in fix_inv.prompt
    assert "test -f marker.txt" in fix_inv.prompt
    # plan and review ran read-only; review got a FRESH session
    assert mocks["plan"].invocations[0].read_only  # type: ignore[attr-defined]
    review_inv = mocks["review"].invocations[0]  # type: ignore[attr-defined]
    assert review_inv.read_only and review_inv.session_id is None
    # review saw the diff
    assert "hello, adw" in review_inv.prompt
    # a commit landed on the work branch and artifacts exist
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=target_repo, capture_output=True, text=True
    ).stdout.strip()
    assert branch == "adw/test-run"
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=target_repo, capture_output=True, text=True
    ).stdout
    assert "add a marker" in log
    assert (ctx.run_dir / "plan.md").read_text().startswith("# Plan")
    assert (ctx.run_dir / "review.md").read_text().startswith("VERDICT")
    assert ctx.state.status == "shipped"
    # .adw artifacts were not swept into the ship commit
    committed = subprocess.run(
        ["git", "ls-files"], cwd=target_repo, capture_output=True, text=True
    ).stdout
    assert ".adw" not in committed


def test_gates_exhausted_fails(target_repo: Path) -> None:
    """If fixes never repair the gates, the run fails after max_fix_iterations."""
    build_mock = MockAdapter(
        [
            ScriptedTurn(output="built", session_id="s", on_invoke=edit_app(target_repo)),
            ScriptedTurn(output="fix 1", session_id="s"),
            ScriptedTurn(output="fix 2", session_id="s"),
        ]
    )
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter([ScriptedTurn(output="plan")]),
        "build": build_mock,
        "review": MockAdapter(),
    }
    ctx = make_ctx(target_repo, make_config(max_fixes=2), mocks)
    outcome = FeatureWorkflow().run(ctx)

    assert outcome.status == "failed"
    assert "fix attempts" in outcome.reason
    assert len(build_mock.invocations) == 3  # 1 build + 2 fixes, then stop
    assert ctx.state.status == "failed"
    assert ctx.state.fix_attempts == 2
    # review never ran
    assert mocks["review"].invocations == []  # type: ignore[attr-defined]


def test_plan_rejection_cleans_up(target_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter([ScriptedTurn(output="plan text")]),
        "build": MockAdapter(),
        "review": MockAdapter(),
    }
    ctx = make_ctx(target_repo, make_config(), mocks)
    ctx.assume_yes = False
    monkeypatch.setattr("adw.human.approve_plan", lambda *a, **k: "reject")
    outcome = FeatureWorkflow().run(ctx)

    assert outcome.status == "rejected"
    assert mocks["build"].invocations == []  # type: ignore[attr-defined]
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=target_repo, capture_output=True, text=True
    ).stdout.strip()
    assert branch == "main"  # work branch deleted, back on base


def test_final_rejection_keeps_branch(target_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter([ScriptedTurn(output="plan")]),
        "build": MockAdapter(
            [ScriptedTurn(output="built", session_id="s", on_invoke=touch_marker(target_repo))]
        ),
        "review": MockAdapter([ScriptedTurn(output="VERDICT: concerns")]),
    }
    ctx = make_ctx(target_repo, make_config(), mocks)
    ctx.assume_yes = False
    ctx.auto_approve_plan = True
    monkeypatch.setattr("adw.human.final_review", lambda *a, **k: False)
    outcome = FeatureWorkflow().run(ctx)

    assert outcome.status == "rejected"
    assert any("adw/test-run" in h for h in outcome.hints)
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=target_repo, capture_output=True, text=True
    ).stdout
    assert "add a marker" not in log  # nothing committed


def test_preflight_dirty_tree_fails_fast(target_repo: Path) -> None:
    (target_repo / "app.py").write_text("dirty\n")
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter(),
        "build": MockAdapter(),
        "review": MockAdapter(),
    }
    ctx = make_ctx(target_repo, make_config(), mocks)
    outcome = FeatureWorkflow().run(ctx)
    assert outcome.status == "failed"
    assert "not clean" in outcome.reason
    assert mocks["plan"].invocations == []  # type: ignore[attr-defined]


def test_plan_agent_failure_fails_run(target_repo: Path) -> None:
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter([ScriptedTurn(ok=False, output="")]),
        "build": MockAdapter(),
        "review": MockAdapter(),
    }
    ctx = make_ctx(target_repo, make_config(), mocks)
    outcome = FeatureWorkflow().run(ctx)
    assert outcome.status == "failed"
    assert "plan agent failed" in outcome.reason


def test_review_ship_first_no_revise(target_repo: Path) -> None:
    """A 'ship' verdict on the first review skips the revise loop entirely."""
    build_mock = MockAdapter(
        [ScriptedTurn(output="built", session_id="build-s1", on_invoke=touch_marker(target_repo))]
    )
    review_mock = MockAdapter([ScriptedTurn(output="VERDICT: ship")])
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter([ScriptedTurn(output="plan")]),
        "build": build_mock,
        "review": review_mock,
    }
    ctx = make_ctx(target_repo, make_config(), mocks)
    outcome = FeatureWorkflow().run(ctx)

    assert outcome.status == "shipped"
    assert len(build_mock.invocations) == 1  # no revise
    assert len(review_mock.invocations) == 1  # no re-review


def test_review_concerns_then_ship(target_repo: Path) -> None:
    """'concerns' routes back to the build session; the next review ships."""
    build_mock = MockAdapter(
        [
            ScriptedTurn(
                output="built", session_id="build-s1", on_invoke=touch_marker(target_repo)
            ),
            ScriptedTurn(output="revised", session_id="build-s1"),
        ]
    )
    review_mock = MockAdapter(
        [
            ScriptedTurn(output="VERDICT: concerns\n- missing null check"),
            ScriptedTurn(output="VERDICT: ship"),
        ]
    )
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter([ScriptedTurn(output="plan")]),
        "build": build_mock,
        "review": review_mock,
    }
    ctx = make_ctx(target_repo, make_config(), mocks)
    outcome = FeatureWorkflow().run(ctx)

    assert outcome.status == "shipped"
    assert len(build_mock.invocations) == 2  # 1 build + 1 revise
    revise_inv = build_mock.invocations[1]
    assert revise_inv.session_id == "build-s1"  # resumed the build session
    assert "missing null check" in revise_inv.prompt  # concerns reached the revise prompt
    assert len(review_mock.invocations) == 2
    assert all(inv.session_id is None for inv in review_mock.invocations)  # fresh sessions
    # the gates re-ran after the revise
    assert any(r.name == "r1-gates-1" and r.status == "ok" for r in ctx.state.steps)
    assert (ctx.run_dir / "review.md").read_text().startswith("VERDICT: ship")


def test_review_iteration_cap(target_repo: Path) -> None:
    """Persistent 'concerns' stops after max_review_iterations revise rounds."""
    build_mock = MockAdapter(
        [
            ScriptedTurn(
                output="built", session_id="build-s1", on_invoke=touch_marker(target_repo)
            ),
            ScriptedTurn(output="revise 1", session_id="build-s1"),
            ScriptedTurn(output="revise 2", session_id="build-s1"),
        ]
    )
    review_mock = MockAdapter(
        [
            ScriptedTurn(output="VERDICT: concerns\n- one"),
            ScriptedTurn(output="VERDICT: concerns\n- two"),
            ScriptedTurn(output="VERDICT: concerns\n- three"),
        ]
    )
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter([ScriptedTurn(output="plan")]),
        "build": build_mock,
        "review": review_mock,
    }
    ctx = make_ctx(target_repo, make_config(max_reviews=2), mocks)
    outcome = FeatureWorkflow().run(ctx)

    assert outcome.status == "shipped"  # assume_yes approves the final gate
    assert len(review_mock.invocations) == 3  # initial + 2 re-reviews
    assert len(build_mock.invocations) == 3  # 1 build + 2 revises (cap respected)
    assert ctx.state.review_rounds == 2


def test_review_unparseable_verdict_ships(target_repo: Path) -> None:
    """A review with no VERDICT line is treated as 'ship' — no loop."""
    build_mock = MockAdapter(
        [ScriptedTurn(output="built", session_id="build-s1", on_invoke=touch_marker(target_repo))]
    )
    review_mock = MockAdapter([ScriptedTurn(output="Looks fine to me.")])
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter([ScriptedTurn(output="plan")]),
        "build": build_mock,
        "review": review_mock,
    }
    ctx = make_ctx(target_repo, make_config(), mocks)
    outcome = FeatureWorkflow().run(ctx)

    assert outcome.status == "shipped"
    assert len(build_mock.invocations) == 1
    assert len(review_mock.invocations) == 1


def make_ship_ctx(repo: Path, config: AdwConfig) -> WorkflowContext:
    """A ctx wired for a direct steps.ship() call, with a real staged change to commit."""
    ctx = make_ctx(repo, config, {})
    # ship() reads state.work_branch/base_branch for its detail string; mirror a real run.
    ctx.state.base_branch = "main"
    ctx.state.work_branch = "main"
    (repo / "app.py").write_text("def hello():\n    return 'shipped'\n")
    return ctx


def test_ship_create_pr_no_remote_still_ships(target_repo: Path) -> None:
    """create_pr on + no git remote: PR is skipped with a warning, the run still ships."""
    config = AdwConfig.model_validate(
        {
            "gates": {"marker": {"command": "test -f marker.txt", "timeout": 10}},
            "workflow": {"gate_order": ["marker"]},
            "ship": {"create_pr": True},
        }
    )
    ctx = make_ship_ctx(target_repo, config)

    outcome = steps.ship(ctx, title="test")

    assert outcome.status == "shipped"
    assert "branch" in outcome.reason
    assert ctx.state.work_branch in outcome.reason
    assert ctx.state.status == "shipped"


def test_ship_create_pr_false_unchanged(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create_pr off (the default): no push/PR attempted, detail is the plain commit line."""

    def _boom(*a: object, **k: object) -> None:
        raise AssertionError("PR code path must not run when create_pr is False")

    monkeypatch.setattr(git_ops, "push_branch", _boom)
    monkeypatch.setattr(git_ops, "create_pr", _boom)

    ctx = make_ship_ctx(target_repo, make_config())
    outcome = steps.ship(ctx, title="test")

    assert outcome.status == "shipped"
    assert "PR" not in outcome.reason
    assert re.fullmatch(rf"commit \w+ on {ctx.state.work_branch}", outcome.reason)


def test_has_remote(target_repo: Path) -> None:
    """has_remote reflects the repo's configured remotes."""
    assert git_ops.has_remote(target_repo) is False
    git_ops._git(target_repo, "remote", "add", "origin", "https://example.com/x.git")
    assert git_ops.has_remote(target_repo) is True
