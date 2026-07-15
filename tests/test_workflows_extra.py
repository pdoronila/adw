"""Tests for the chore, bug, hotfix, and cve workflows (MockAdapters, real git)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from adw.adapters.base import AgentAdapter, AgentInvocation
from adw.adapters.mock import MockAdapter, ScriptedTurn
from adw.config import AdwConfig
from adw.nodes.agent_node import AgentRunner
from adw.state.run_state import RunState, create_run_dir, save_state
from adw.workflows.base import WorkflowContext
from adw.workflows.bug import BugWorkflow
from adw.workflows.chore import ChoreWorkflow
from adw.workflows.cve import CveWorkflow
from adw.workflows.hotfix import HotfixWorkflow


def make_config(max_fixes: int = 2) -> AdwConfig:
    return AdwConfig.model_validate(
        {
            "gates": {"marker": {"command": "test -f marker.txt", "timeout": 10}},
            "workflow": {"max_fix_iterations": max_fixes, "gate_order": ["marker"]},
        }
    )


def make_ctx(repo: Path, mocks: dict[str, AgentAdapter], task: str = "do it") -> WorkflowContext:
    run_dir = create_run_dir(repo, "test-run")
    state = RunState(run_id="test-run", workflow="x", task=task, repo=str(repo))
    save_state(state, run_dir)
    return WorkflowContext(
        repo_dir=repo,
        run_dir=run_dir,
        config=make_config(),
        state=state,
        task=task,
        agents=AgentRunner(
            make_config(), run_dir, adapter_factory=lambda role, backend: mocks[role]
        ),
        assume_yes=True,
    )


def make_marker(repo: Path):
    def _do(inv: AgentInvocation) -> None:
        (repo / "marker.txt").write_text("done\n")

    return _do


def committed_files(repo: Path) -> str:
    return subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, text=True
    ).stdout


def test_chore_uses_single_agent_no_plan_or_review(target_repo: Path) -> None:
    build = MockAdapter([ScriptedTurn(output="did chore", on_invoke=make_marker(target_repo))])
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter(),
        "build": build,
        "review": MockAdapter(),
    }
    ctx = make_ctx(target_repo, mocks, task="bump dep")
    outcome = ChoreWorkflow().run(ctx)

    assert outcome.status == "shipped"
    # only the build agent ran — no plan, no review
    assert len(build.invocations) == 1
    assert mocks["plan"].invocations == []  # type: ignore[attr-defined]
    assert mocks["review"].invocations == []  # type: ignore[attr-defined]
    assert "marker.txt" in committed_files(target_repo)


def test_bug_fix_flow_ships(target_repo: Path) -> None:
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter([ScriptedTurn(output="# Diagnosis\nroot cause X", session_id="d")]),
        "build": MockAdapter(
            [ScriptedTurn(output="fixed + test", on_invoke=make_marker(target_repo))]
        ),
        "review": MockAdapter([ScriptedTurn(output="VERDICT: ship")]),
    }
    ctx = make_ctx(target_repo, mocks, task="crash on empty input")
    outcome = BugWorkflow().run(ctx)

    assert outcome.status == "shipped"
    # diagnose ran read-only; build got the diagnosis text in its prompt
    assert mocks["plan"].invocations[0].read_only  # type: ignore[attr-defined]
    assert "root cause X" in mocks["build"].invocations[0].prompt  # type: ignore[attr-defined]
    assert (ctx.run_dir / "plan.md").read_text().startswith("# Diagnosis")


def test_hotfix_requires_approval_then_ships(target_repo: Path) -> None:
    scout = MockAdapter([ScriptedTurn(output="# Surgical fix\npatch line 10", session_id="s")])
    build = MockAdapter([ScriptedTurn(output="patched", on_invoke=make_marker(target_repo))])
    mocks: dict[str, AgentAdapter] = {"plan": scout, "build": build, "review": MockAdapter()}
    ctx = make_ctx(target_repo, mocks, task="prod 500s on /login")
    outcome = HotfixWorkflow().run(ctx)

    assert outcome.status == "shipped"
    assert scout.invocations[0].read_only  # scout is read-only
    assert "patch line 10" in build.invocations[0].prompt  # approved fix fed to build
    # no review agent in the hotfix path
    assert mocks["review"].invocations == []  # type: ignore[attr-defined]


def test_cve_reproduce_then_mitigate_same_session(target_repo: Path) -> None:
    reproduce_and_mitigate = MockAdapter(
        [
            ScriptedTurn(output="added failing security test", session_id="cve-sess"),
            ScriptedTurn(output="implemented protection", session_id="cve-sess",
                         on_invoke=make_marker(target_repo)),
        ]
    )
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter([ScriptedTurn(output="# CVE-2024-9999 analysis\npath traversal")]),
        "build": reproduce_and_mitigate,
        "review": MockAdapter([ScriptedTurn(output="VERDICT: ship")]),
    }
    ctx = make_ctx(target_repo, mocks, task="CVE-2024-9999 path traversal in file loader")
    outcome = CveWorkflow().run(ctx)

    assert outcome.status == "shipped"
    # research ran read-only
    assert mocks["plan"].invocations[0].read_only  # type: ignore[attr-defined]
    # reproduce opened the session; mitigate RESUMED the same one
    reproduce_inv, mitigate_inv = reproduce_and_mitigate.invocations
    assert reproduce_inv.session_id is None
    assert mitigate_inv.session_id == "cve-sess"
    # transcripts recorded in order: research, reproduce, mitigate, review
    names = [p.name.split("-", 1)[1] for p in sorted((ctx.run_dir / "agent").iterdir())]
    assert names == ["research.json", "reproduce.json", "mitigate.json", "review.json"]


def test_cve_research_rejection_writes_nothing(target_repo: Path, monkeypatch) -> None:
    mocks: dict[str, AgentAdapter] = {
        "plan": MockAdapter([ScriptedTurn(output="analysis")]),
        "build": MockAdapter(),
        "review": MockAdapter(),
    }
    ctx = make_ctx(target_repo, mocks, task="CVE-x")
    ctx.assume_yes = False
    monkeypatch.setattr("adw.human.approve_plan", lambda *a, **k: "reject")
    outcome = CveWorkflow().run(ctx)

    assert outcome.status == "rejected"
    assert mocks["build"].invocations == []  # type: ignore[attr-defined]
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=target_repo, capture_output=True, text=True
    ).stdout.strip()
    assert branch == "main"
