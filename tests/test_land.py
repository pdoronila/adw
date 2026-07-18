"""The land step: rebase the work branch onto the base branch and ff-merge it."""

from __future__ import annotations

import subprocess
from pathlib import Path

from adw.adapters.mock import MockAdapter
from adw.config import AdwConfig
from adw.nodes import git_ops
from adw.nodes.agent_node import AgentRunner
from adw.state.run_state import RunState, create_run_dir, save_state
from adw.workflows import steps
from adw.workflows.base import WorkflowContext


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _make_branches(repo: Path, *, conflict: bool = False) -> None:
    """A `work` branch with one commit, then `main` advanced by another commit."""
    _run(repo, "checkout", "-b", "work")
    (repo / "app.py").write_text("def hello():\n    return 'work'\n")
    _run(repo, "commit", "-am", "work change")
    _run(repo, "checkout", "main")
    if conflict:
        (repo / "app.py").write_text("def hello():\n    return 'main'\n")
        _run(repo, "commit", "-am", "main change")
    else:
        (repo / "other.py").write_text("x = 1\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-m", "main advance")


def _make_state(repo: Path) -> RunState:
    state = RunState(run_id="land-run", workflow="feature", task="t", repo=str(repo))
    state.base_branch = "main"
    state.work_branch = "work"
    state.status = "shipped"
    return state


def _build_ctx(repo: Path, *, land: bool) -> WorkflowContext:
    """A context for driving ship() directly — agents are never invoked by ship."""
    git_ops.ensure_adw_ignored(repo)
    config = AdwConfig.model_validate(
        {
            "gates": {"lint": {"command": "true", "timeout": 10}},
            "ship": {"land": land},
        }
    )
    run_dir = create_run_dir(repo, "land-run")
    state = _make_state(repo)
    state.status = "running"
    save_state(state, run_dir)
    return WorkflowContext(
        repo_dir=repo,
        run_dir=run_dir,
        config=config,
        state=state,
        task="t",
        agents=AgentRunner(
            config,
            run_dir,
            adapter_factory=lambda role, backend: MockAdapter(),
            workflow="feature",
        ),
    )


def test_land_success(target_repo: Path) -> None:
    _make_branches(target_repo)
    state = _make_state(target_repo)

    landed, detail = steps.land(state)

    assert landed
    assert "landed on main" in detail
    assert "work change" in _out(target_repo, "log", "main", "--oneline")
    assert git_ops.current_branch(target_repo) == "main"
    assert not git_ops.branch_exists(target_repo, "work")


def test_land_conflict_keeps_branch(target_repo: Path) -> None:
    _make_branches(target_repo, conflict=True)
    before = _out(target_repo, "rev-parse", "work").strip()
    state = _make_state(target_repo)

    landed, detail = steps.land(state)

    assert not landed
    assert "manual land needed" in detail
    assert git_ops.branch_exists(target_repo, "work")
    assert _out(target_repo, "rev-parse", "work").strip() == before


def test_ship_lands_when_configured(target_repo: Path) -> None:
    _make_branches(target_repo)
    # checkout `work` with uncommitted build output, matching local isolation
    _run(target_repo, "checkout", "work")
    (target_repo / "extra.py").write_text("y = 2\n")
    ctx = _build_ctx(target_repo, land=True)

    outcome = steps.ship(ctx)

    assert outcome.status == "shipped"
    assert ctx.state.status == "shipped"
    assert "landed on main" in ctx.state.outcome_detail
    assert "work change" in _out(target_repo, "log", "main", "--oneline")
    assert not git_ops.branch_exists(target_repo, "work")


def test_ship_land_conflict_still_ships(target_repo: Path) -> None:
    _make_branches(target_repo, conflict=True)
    _run(target_repo, "checkout", "work")
    (target_repo / "extra.py").write_text("y = 2\n")
    ctx = _build_ctx(target_repo, land=True)

    outcome = steps.ship(ctx)

    assert outcome.status == "shipped"
    assert ctx.state.status == "shipped"
    assert "manual land needed" in ctx.state.outcome_detail
    assert git_ops.branch_exists(target_repo, "work")
