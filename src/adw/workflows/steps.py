"""Reusable ADW steps.

Each workflow is a short composition of these. A step returns `RunOutcome | None`:
non-None means "stop now and return this" (a failure or rejection); None means
"continue". Code owns orchestration; agents own the fuzzy work; the engineer
owns the two human gates.
"""

from __future__ import annotations

import typer

from adw import human, prompts
from adw.nodes import code_node, git_ops
from adw.nodes.code_node import GateResult
from adw.state.run_state import save_state
from adw.workflows.base import RunOutcome, WorkflowContext


def fail(
    ctx: WorkflowContext, step: str, reason: str, hints: list[str] | None = None
) -> RunOutcome:
    ctx.state.status = "failed"
    ctx.state.outcome_detail = f"{step}: {reason}"
    save_state(ctx.state, ctx.run_dir)
    return RunOutcome("failed", reason, hints=hints or [])


def preflight(ctx: WorkflowContext, *, require_clean: bool = True) -> RunOutcome | None:
    problems: list[str] = []
    if not git_ops.is_git_repo(ctx.repo_dir):
        problems.append(f"{ctx.repo_dir} is not a git repository")
    else:
        # Keep adw's own .adw/ artifacts out of status/diff/add from here on.
        git_ops.ensure_adw_ignored(ctx.repo_dir)
        # Only require a clean tree on a fresh run — a resume legitimately carries
        # the build's uncommitted work.
        resuming = ctx.state.step("branch").status == "ok"
        if require_clean and not resuming and not git_ops.ensure_clean(ctx.repo_dir):
            problems.append("working tree is not clean; commit or stash first")
    if not ctx.config.gates:
        problems.append("no gates configured in adw.yaml")
    else:
        missing = [g for g in ctx.config.gate_order() if g not in ctx.config.gates]
        if missing:
            problems.append(f"gate_order names undefined gates: {missing}")
    if problems:
        return fail(ctx, "preflight", "; ".join(problems))
    return None


def _done(ctx: WorkflowContext, step_name: str) -> bool:
    """True if a step already completed — used to skip work when resuming a paused run."""
    return ctx.state.step(step_name).status in ("ok", "skipped")


def start_branch(ctx: WorkflowContext) -> None:
    state = ctx.state
    if _done(ctx, "branch"):
        git_ops.checkout(ctx.repo_dir, state.work_branch)  # resuming: branch already exists
        return
    state.base_branch = git_ops.current_branch(ctx.repo_dir)
    state.work_branch = f"{ctx.config.ship.branch_prefix}{state.run_id}"
    state.start_step("branch")
    git_ops.create_branch(ctx.repo_dir, state.work_branch)
    state.end_step("branch", "ok", state.work_branch)
    save_state(state, ctx.run_dir)


def agent_doc(
    ctx: WorkflowContext,
    *,
    role: str,
    step_name: str,
    prompt_text: str,
    out_name: str,
    read_only: bool = True,
) -> RunOutcome | None:
    """Run an agent whose deliverable is a document (plan/analysis/repro notes).

    Writes the agent's final message to run_dir/out_name.
    """
    state = ctx.state
    if _done(ctx, step_name):
        return None  # resuming: the document is already on disk
    state.start_step(step_name)
    result = ctx.agents.run(
        role, prompt_text, cwd=ctx.repo_dir, step_name=step_name, read_only=read_only
    )
    state.add_cost(result.cost_usd)
    if not result.ok or not result.output.strip():
        state.end_step(step_name, "failed", result.error)
        return fail(ctx, step_name, f"{role} agent failed: {result.error}")
    (ctx.run_dir / out_name).write_text(result.output)
    state.end_step(step_name, "ok", _session_note(result.session_id))
    save_state(state, ctx.run_dir)
    return None


def _decide(ctx: WorkflowContext, *, kind: str, artifact_name: str) -> str | None:
    """Return 'approve'/'reject', or None to pause (async mode with no decision).

    Precedence: auto flags → a decision injected by `adw resume` → interactive
    prompt (blocking) → pause (async).
    """
    if ctx.assume_yes or (kind == "plan" and ctx.auto_approve_plan):
        return "approve"
    if ctx.decision is not None:
        decision, ctx.decision = ctx.decision, None  # consume it (answers one gate)
        return decision
    if ctx.mode == "async":
        return None
    if kind == "plan":
        return human.approve_plan(ctx.run_dir / artifact_name)
    summary = git_ops.diff_summary(ctx.repo_dir, ctx.state.base_branch)
    return "approve" if human.final_review(summary, ctx.run_dir / artifact_name) else "reject"


def approve_gate(
    ctx: WorkflowContext,
    artifact_name: str,
    *,
    reject_reason: str = "rejected by engineer",
) -> RunOutcome | None:
    """Engineer gate 1: approve/reject the document (pauses in async mode)."""
    state = ctx.state
    if _done(ctx, "approve"):
        return None  # already approved in a prior (paused) run
    decision = _decide(ctx, kind="plan", artifact_name=artifact_name)
    if decision is None:
        state.status = "awaiting_plan_approval"
        state.pending_gate = "plan"
        save_state(state, ctx.run_dir)
        return RunOutcome(
            "paused",
            "awaiting plan approval",
            hints=[
                f"review {ctx.run_dir / artifact_name}",
                f"adw resume {state.run_id} --approve   (or --reject)",
            ],
        )
    if decision != "approve":
        git_ops.checkout(ctx.repo_dir, state.base_branch)
        git_ops.delete_branch(ctx.repo_dir, state.work_branch)
        state.status = "rejected"
        state.pending_gate = None
        state.end_step("approve", "failed", "rejected")
        state.outcome_detail = reject_reason
        save_state(state, ctx.run_dir)
        return RunOutcome("rejected", reject_reason)
    state.end_step("approve", "ok")
    state.status = "running"
    state.pending_gate = None
    save_state(state, ctx.run_dir)
    return None


def build(
    ctx: WorkflowContext,
    prompt_text: str,
    *,
    role: str = "build",
    step_name: str = "build",
) -> RunOutcome | None:
    """A write-access agent turn that starts THE session the gate loop resumes."""
    state = ctx.state
    if _done(ctx, step_name):
        return None  # resuming: build already ran (session id is persisted)
    state.start_step(step_name)
    result = ctx.agents.run(role, prompt_text, cwd=ctx.repo_dir, step_name=step_name)
    state.add_cost(result.cost_usd)
    state.build_session_id = result.session_id
    if not result.ok:
        state.end_step(step_name, "failed", result.error)
        return fail(ctx, step_name, f"{role} agent failed: {result.error}")
    state.end_step(step_name, "ok", _session_note(result.session_id))
    save_state(state, ctx.run_dir)
    return None


def resume_turn(
    ctx: WorkflowContext,
    prompt_text: str,
    *,
    role: str = "build",
    step_name: str,
) -> RunOutcome | None:
    """A write-access agent turn that RESUMES the build session (keeps context)."""
    state = ctx.state
    if _done(ctx, step_name):
        return None
    state.start_step(step_name)
    result = ctx.agents.run(
        role, prompt_text, cwd=ctx.repo_dir, step_name=step_name, session_id=state.build_session_id
    )
    state.add_cost(result.cost_usd)
    if not result.ok:
        state.end_step(step_name, "failed", result.error)
        return fail(ctx, step_name, f"{role} agent failed: {result.error}")
    if result.session_id:
        state.build_session_id = result.session_id
    state.end_step(step_name, "ok")
    save_state(state, ctx.run_dir)
    return None


def gate_loop(ctx: WorkflowContext, *, role: str = "build") -> RunOutcome | None:
    """Run all gates; on failure, resume the build session with the failures. Repeat."""
    state, config, repo = ctx.state, ctx.config, ctx.repo_dir
    if state.gates_passed:
        return None  # resuming after gates already passed
    gate_order = config.gate_order()
    gates_dir = ctx.run_dir / "gates"
    max_fixes = config.workflow.max_fix_iterations
    passed = False
    for attempt in range(1, max_fixes + 2):
        state.start_step(f"gates-{attempt}")
        results = code_node.run_gates(gate_order, config.gates, repo, gates_dir, attempt)
        state.gate_results.append(
            {
                "attempt": attempt,
                "results": [
                    {"name": r.name, "ok": r.ok, "exit_code": r.exit_code} for r in results
                ],
            }
        )
        failures = [r for r in results if not r.ok]
        if not failures:
            state.end_step(f"gates-{attempt}", "ok")
            state.gates_passed = True
            save_state(state, ctx.run_dir)
            passed = True
            break
        names = ", ".join(f.name for f in failures)
        state.end_step(f"gates-{attempt}", "failed", f"failed: {names}")
        save_state(state, ctx.run_dir)
        if attempt > max_fixes:
            break
        typer.secho(
            f"gates failed ({names}); routing back to {role} agent [fix {attempt}/{max_fixes}]",
            fg="yellow",
        )
        state.fix_attempts = attempt
        outcome = resume_turn(
            ctx,
            prompts.render("fix", failures=render_failures(failures)),
            role=role,
            step_name=f"fix-{attempt}",
        )
        if outcome is not None:
            return outcome
    if not passed:
        return fail(
            ctx,
            "gates",
            f"gates still failing after {max_fixes} fix attempts",
            hints=[f"inspect logs: {gates_dir}", f"adw status {state.run_id}"],
        )
    return None


def review(ctx: WorkflowContext, *, context: str, prompt_name: str = "review") -> None:
    """Fresh-session, read-only review of the diff. Advisory; writes review.md."""
    repo = ctx.repo_dir
    state = ctx.state
    if _done(ctx, "review"):
        return  # resuming: review already ran
    git_ops.stage_all(repo)  # so new files appear in the diff vs the base
    state.start_step("review")
    result = ctx.agents.run(
        "review",
        prompts.render(
            prompt_name,
            context=context,
            diff=code_node.truncate_middle(git_ops.full_diff(repo, state.base_branch)),
        ),
        cwd=repo,
        step_name="review",
        read_only=True,
    )
    state.add_cost(result.cost_usd)
    if result.ok and result.output.strip():
        (ctx.run_dir / "review.md").write_text(result.output)
        state.end_step("review", "ok")
    else:
        state.end_step("review", "skipped", f"review agent failed: {result.error}")
    state.status = "awaiting_final_review"
    save_state(state, ctx.run_dir)


def final_gate(ctx: WorkflowContext) -> RunOutcome | None:
    """Engineer gate 2: ship or reject (pauses in async mode; reject keeps the branch)."""
    state = ctx.state
    if _done(ctx, "final"):
        return None
    decision = _decide(ctx, kind="final", artifact_name="review.md")
    if decision is None:
        state.status = "awaiting_final_review"
        state.pending_gate = "final"
        save_state(state, ctx.run_dir)
        return RunOutcome(
            "paused",
            "awaiting final review",
            hints=[
                f"inspect the change: adw status {state.run_id}",
                f"adw resume {state.run_id} --approve   (or --reject)",
            ],
        )
    if decision != "approve":
        state.status = "rejected"
        state.pending_gate = None
        state.end_step("final", "failed", "rejected")
        state.outcome_detail = f"rejected at final review; branch {state.work_branch} kept"
        save_state(state, ctx.run_dir)
        return RunOutcome(
            "rejected",
            "rejected at final review",
            hints=[f"work preserved on branch {state.work_branch}"],
        )
    state.end_step("final", "ok")
    state.pending_gate = None
    save_state(state, ctx.run_dir)
    return None


def ship(ctx: WorkflowContext, *, title: str | None = None) -> RunOutcome:
    state, config, repo = ctx.state, ctx.config, ctx.repo_dir
    git_ops.stage_all(repo)
    state.start_step("ship")
    commit = git_ops.commit_all(repo, f"{title or ctx.task}\n\nadw-run: {state.run_id}")
    detail = f"commit {commit} on {state.work_branch}"
    if config.ship.create_pr:
        summary = git_ops.diff_summary(repo, state.base_branch)
        pr_url = git_ops.create_pr(
            repo, title or ctx.task, f"Automated by adw run {state.run_id}\n\n{summary}"
        )
        detail += f"; PR {pr_url}"
    state.end_step("ship", "ok", detail)
    state.status = "shipped"
    state.outcome_detail = detail
    save_state(state, ctx.run_dir)
    return RunOutcome("shipped", detail)


def render_failures(failures: list[GateResult]) -> str:
    blocks = []
    for f in failures:
        blocks.append(
            f"## Gate `{f.name}` failed (exit {f.exit_code})\n"
            f"Command: `{f.command}`\n\n"
            f"```\n{f.output_excerpt}\n```"
        )
    return "\n\n".join(blocks)


def _session_note(session_id: str | None) -> str:
    return f"session {session_id}" if session_id else ""
