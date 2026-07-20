"""Fusion workflow tests: parallel opinions, fused plan, generated validation gate."""

from __future__ import annotations

import time
from pathlib import Path

from adw.adapters.base import AgentAdapter, AgentInvocation
from adw.adapters.mock import MockAdapter, ScriptedTurn
from adw.config import AdwConfig
from adw.nodes.agent_node import AgentRunner
from adw.state.run_state import RunState, create_run_dir, load_state, runs_root, save_state
from adw.workflows import steps
from adw.workflows.base import WorkflowContext
from adw.workflows.fusion import FusionWorkflow

VALIDATOR_SCRIPT = 'test -f marker.txt || { echo "FAIL: marker missing"; exit 1; }'
VALIDATOR_OUTPUT = f"```bash\n{VALIDATOR_SCRIPT}\n```"


def make_config(
    max_validate: int = 2, gate_command: str = "test -f marker.txt", extra: dict | None = None
) -> AdwConfig:
    data = {
        "gates": {"marker": {"command": gate_command, "timeout": 10}},
        "workflow": {
            "max_fix_iterations": 3,
            "max_review_iterations": 2,
            "gate_order": ["marker"],
        },
        "fusion": {
            "opinions": ["opinion_a", "opinion_b"],
            "max_validate_iterations": max_validate,
            "validate_timeout": 10,
        },
    }
    if extra:
        data.update(extra)
    return AdwConfig.model_validate(data)


def make_ctx(
    repo: Path,
    config: AdwConfig,
    mocks: dict[str, AgentAdapter],
    *,
    mode: str = "interactive",
    decision: str | None = None,
    assume_yes: bool = True,
) -> WorkflowContext:
    """Reuses an existing run dir (resume) or creates a fresh one."""
    run_dir = runs_root(repo) / "test-run"
    if (run_dir / "state.json").is_file():
        state = load_state(run_dir)
    else:
        run_dir = create_run_dir(repo, "test-run")
        state = RunState(run_id="test-run", workflow="fusion", task="add a marker", repo=str(repo))
        save_state(state, run_dir)

    def factory(role: str, backend: str) -> AgentAdapter:
        return mocks.setdefault(role, MockAdapter())

    return WorkflowContext(
        repo_dir=repo,
        run_dir=run_dir,
        config=config,
        state=state,
        task="add a marker",
        agents=AgentRunner(config, run_dir, adapter_factory=factory, workflow="fusion"),
        assume_yes=assume_yes,
        mode=mode,  # type: ignore[arg-type]
        decision=decision,  # type: ignore[arg-type]
    )


def touch_marker(repo: Path):
    def _do(inv: AgentInvocation) -> None:
        (repo / "marker.txt").write_text("fixed\n")

    return _do


def edit_app(repo: Path):
    def _do(inv: AgentInvocation) -> None:
        (repo / "app.py").write_text("def hello():\n    return 'hello, adw'\n")

    return _do


def happy_mocks(repo: Path) -> dict[str, AgentAdapter]:
    return {
        "opinion_a": MockAdapter([ScriptedTurn(output="opinion A: do X", session_id="oa")]),
        "opinion_b": MockAdapter([ScriptedTurn(output="opinion B: do Y", session_id="ob")]),
        "fusion": MockAdapter(
            [ScriptedTurn(output="## Consensus\nadd it\n## Fused plan\nadd marker.txt")]
        ),
        "validator": MockAdapter([ScriptedTurn(output=VALIDATOR_OUTPUT)]),
        "build": MockAdapter(
            [ScriptedTurn(output="built", session_id="b1", on_invoke=touch_marker(repo))]
        ),
        "review": MockAdapter([ScriptedTurn(output="VERDICT: ship")]),
    }


def gate_names(state: RunState) -> list[str]:
    return [
        str(r["name"])
        for round_ in state.gate_results
        for r in round_["results"]  # type: ignore[union-attr]
    ]


def test_happy_path(target_repo: Path) -> None:
    """Opinions fan out read-only, fusion sees both, validator gate passes, ships."""
    mocks = happy_mocks(target_repo)
    ctx = make_ctx(target_repo, make_config(), mocks)
    outcome = FusionWorkflow().run(ctx)

    assert outcome.status == "shipped"
    # both opinions ran read-only in fresh sessions
    for role in ("opinion_a", "opinion_b"):
        inv = mocks[role].invocations[0]  # type: ignore[attr-defined]
        assert inv.read_only and inv.session_id is None
    # both opinions reached the fusion prompt
    fusion_prompt = mocks["fusion"].invocations[0].prompt  # type: ignore[attr-defined]
    assert "opinion A: do X" in fusion_prompt
    assert "opinion B: do Y" in fusion_prompt
    # the validator ran read-only and got the fused plan
    validator_inv = mocks["validator"].invocations[0]  # type: ignore[attr-defined]
    assert validator_inv.read_only
    assert "add marker.txt" in validator_inv.prompt
    # artifacts
    opinions = (ctx.run_dir / "opinions.md").read_text()
    assert "| role | backend | model |" in opinions
    assert "## Opinion: opinion_a" in opinions and "## Opinion: opinion_b" in opinions
    assert (ctx.run_dir / "fusion.md").read_text().startswith("## Consensus")
    assert (ctx.run_dir / "validate.sh").read_text().strip() == VALIDATOR_SCRIPT
    # the generated gate ran alongside the configured gates
    assert "validate" in gate_names(ctx.state)
    assert "marker" in gate_names(ctx.state)
    # two opinion transcripts with distinct indices
    transcripts = sorted(
        p.name for p in (ctx.run_dir / "agent").iterdir() if "opinion" in p.name
    )
    assert len(transcripts) == 2
    assert len({name.split("-")[0] for name in transcripts}) == 2
    assert ctx.state.status == "shipped"


def test_validate_failure_loops_back_to_build_session(target_repo: Path) -> None:
    """A failing validation script resumes the build session with FAIL feedback."""
    build_mock = MockAdapter(
        [
            ScriptedTurn(output="built", session_id="b1", on_invoke=edit_app(target_repo)),
            ScriptedTurn(output="fixed", session_id="b1", on_invoke=touch_marker(target_repo)),
        ]
    )
    mocks = happy_mocks(target_repo)
    mocks["build"] = build_mock
    ctx = make_ctx(target_repo, make_config(), mocks)
    outcome = FusionWorkflow().run(ctx)

    assert outcome.status == "shipped"
    assert len(build_mock.invocations) == 2
    fix_inv = build_mock.invocations[1]
    assert fix_inv.session_id == "b1"  # resumed the SAME build session
    assert "Gate `validate` failed" in fix_inv.prompt
    assert "FAIL: marker missing" in fix_inv.prompt  # the script's actionable feedback
    assert any(r.name == "v-fix-1" and r.status == "ok" for r in ctx.state.steps)


def test_validate_exhaustion_fails(target_repo: Path) -> None:
    """A builder that never satisfies the script fails after max_validate_iterations."""
    build_mock = MockAdapter(
        [
            ScriptedTurn(output="built", session_id="b1", on_invoke=edit_app(target_repo)),
            ScriptedTurn(output="fix 1", session_id="b1"),
            ScriptedTurn(output="fix 2", session_id="b1"),
        ]
    )
    mocks = happy_mocks(target_repo)
    mocks["build"] = build_mock
    ctx = make_ctx(target_repo, make_config(max_validate=2), mocks)
    outcome = FusionWorkflow().run(ctx)

    assert outcome.status == "failed"
    assert "fix attempts" in outcome.reason
    assert len(build_mock.invocations) == 1 + 2  # 1 build + max_validate_iterations fixes
    assert mocks["review"].invocations == []  # type: ignore[attr-defined]


def test_builder_tampering_with_gate_script_is_overwritten(target_repo: Path) -> None:
    """The canonical script is re-copied before every attempt, defeating tampering."""

    def tamper(inv: AgentInvocation) -> None:
        adw_dir = target_repo / ".adw"
        adw_dir.mkdir(exist_ok=True)
        (adw_dir / "validate.sh").write_text("exit 0\n")  # and no marker.txt

    build_mock = MockAdapter([ScriptedTurn(output="built", session_id="b1", on_invoke=tamper)])
    mocks = happy_mocks(target_repo)
    mocks["build"] = build_mock
    # a trivially-green configured gate, so only the generated gate can fail
    ctx = make_ctx(target_repo, make_config(max_validate=1, gate_command="true"), mocks)
    outcome = FusionWorkflow().run(ctx)

    assert outcome.status == "failed"  # the tampered exit-0 script never ran
    assert "validation still failing" in outcome.reason
    # the canonical script was re-copied over the tampered one
    assert (target_repo / ".adw" / "validate.sh").read_text().strip() == VALIDATOR_SCRIPT


def test_one_opinion_failure_degrades_gracefully(target_repo: Path) -> None:
    """One failed opinion agent: warn and continue with the survivors."""
    mocks = happy_mocks(target_repo)
    mocks["opinion_b"] = MockAdapter([ScriptedTurn(ok=False, output="")])
    ctx = make_ctx(target_repo, make_config(), mocks)
    outcome = FusionWorkflow().run(ctx)

    assert outcome.status == "shipped"
    opinions = (ctx.run_dir / "opinions.md").read_text()
    assert "## Opinion: opinion_a" in opinions
    assert "## Opinion: opinion_b" not in opinions
    assert ctx.state.step("opinion-opinion_b").status == "failed"


def test_all_opinions_fail_aborts(target_repo: Path) -> None:
    mocks = happy_mocks(target_repo)
    mocks["opinion_a"] = MockAdapter([ScriptedTurn(ok=False, output="")])
    mocks["opinion_b"] = MockAdapter([ScriptedTurn(ok=False, output="")])
    ctx = make_ctx(target_repo, make_config(), mocks)
    outcome = FusionWorkflow().run(ctx)

    assert outcome.status == "failed"
    assert "opinion" in outcome.reason
    assert mocks["fusion"].invocations == []  # type: ignore[attr-defined]


def test_async_pauses_at_fused_plan_then_resumes(target_repo: Path) -> None:
    """Async mode pauses at plan approval; resume completes without re-running opinions."""
    mocks = happy_mocks(target_repo)

    # 1) pause at the fused-plan gate — opinions + fusion ran, validator/build did not
    ctx = make_ctx(target_repo, make_config(), mocks, mode="async", assume_yes=False)
    out = FusionWorkflow().run(ctx)
    assert out.status == "paused"
    assert load_state(ctx.run_dir).pending_gate == "plan"
    assert (ctx.run_dir / "fusion.md").is_file()
    assert mocks["validator"].invocations == []  # type: ignore[attr-defined]
    assert mocks["build"].invocations == []  # type: ignore[attr-defined]

    # 2) approve: validator + build + validate loop + review run, pause at final gate
    ctx = make_ctx(
        target_repo, make_config(), mocks, mode="async", assume_yes=False, decision="approve"
    )
    out = FusionWorkflow().run(ctx)
    assert out.status == "paused"
    assert load_state(ctx.run_dir).pending_gate == "final"

    # 3) approve the final gate: ships; each opinion mock was invoked exactly once
    ctx = make_ctx(
        target_repo, make_config(), mocks, mode="async", assume_yes=False, decision="approve"
    )
    out = FusionWorkflow().run(ctx)
    assert out.status == "shipped"
    assert len(mocks["opinion_a"].invocations) == 1  # type: ignore[attr-defined]
    assert len(mocks["opinion_b"].invocations) == 1  # type: ignore[attr-defined]
    assert len(mocks["fusion"].invocations) == 1  # type: ignore[attr-defined]


def test_validator_prose_is_stripped_to_the_script(target_repo: Path) -> None:
    """Prose around the fenced block never reaches validate.sh."""
    mocks = happy_mocks(target_repo)
    mocks["validator"] = MockAdapter(
        [ScriptedTurn(output=f"Here is the script:\n\n{VALIDATOR_OUTPUT}\n\nGood luck!")]
    )
    ctx = make_ctx(target_repo, make_config(), mocks)
    outcome = FusionWorkflow().run(ctx)

    assert outcome.status == "shipped"
    assert (ctx.run_dir / "validate.sh").read_text() == VALIDATOR_SCRIPT + "\n"


def test_opinion_fanout_parallel_transcripts(target_repo: Path) -> None:
    """4 concurrent opinions persist 4 transcripts with unique indices (persist lock)."""
    roles = ["o1", "o2", "o3", "o4"]
    config = make_config(
        extra={
            "fusion": {"opinions": roles, "max_validate_iterations": 2, "validate_timeout": 10}
        }
    )

    def slow(inv: AgentInvocation) -> None:
        time.sleep(0.01)

    mocks: dict[str, AgentAdapter] = {
        role: MockAdapter([ScriptedTurn(output=f"opinion {role}", on_invoke=slow)])
        for role in roles
    }
    ctx = make_ctx(target_repo, config, mocks)
    outcome = steps.opinion_fanout(ctx, roles=roles, task="add a marker")

    assert outcome is None
    names = sorted(p.name for p in (ctx.run_dir / "agent").iterdir())
    assert len(names) == 4
    assert len({name.split("-")[0] for name in names}) == 4  # unique NN- prefixes
    assert all(ctx.state.step(f"opinion-{role}").status == "ok" for role in roles)
    opinions = (ctx.run_dir / "opinions.md").read_text()
    assert all(f"## Opinion: {role}" in opinions for role in roles)
