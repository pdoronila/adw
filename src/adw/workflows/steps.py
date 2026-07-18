"""Reusable ADW steps.

Each workflow is a short composition of these. A step returns `RunOutcome | None`:
non-None means "stop now and return this" (a failure or rejection); None means
"continue". Code owns orchestration; agents own the fuzzy work; the engineer
owns the two human gates.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import cast

import typer

from adw import human, prompts
from adw.nodes import code_node, git_ops
from adw.nodes.code_node import GateResult
from adw.notify import notify
from adw.queue.tickets import write_ticket
from adw.state.run_state import RunState, save_state
from adw.workflows.base import RunOutcome, WorkflowContext


def fail(
    ctx: WorkflowContext, step: str, reason: str, hints: list[str] | None = None
) -> RunOutcome:
    ctx.state.status = "failed"
    ctx.state.outcome_detail = f"{step}: {reason}"
    _file_failure_ticket(ctx)
    save_state(ctx.state, ctx.run_dir)
    notify(ctx.state, ctx.config)
    return RunOutcome("failed", reason, hints=hints or [])


def _file_failure_ticket(ctx: WorkflowContext) -> None:
    """Auto-file an investigation ticket for a failed run (opt-in via queue.file_failures).

    A run spawned from such a ticket carries `source_ticket_run`, which short-circuits
    here so an unfixable failure can never spawn an unbounded chain of tickets.
    """
    if not ctx.config.queue.file_failures or ctx.state.source_ticket_run is not None:
        return
    try:
        state = ctx.state
        title = f"Investigate failed run {state.run_id}: {state.outcome_detail[:80]}"
        body_lines = [
            f"- run_id: {state.run_id}",
            f"- workflow: {state.workflow}",
            f"- outcome_detail: {state.outcome_detail}",
            "",
            "## Task",
            state.task,
        ]
        tail = _failing_gate_log_tail(state, ctx.run_dir)
        if tail is not None:
            filename, log_text = tail
            body_lines += [
                "",
                f"## Failing gate log (last 40 lines of {filename})",
                "```text",
                log_text,
                "```",
            ]
        # Tickets live in the MAIN repo; under worktree isolation ctx.repo_dir is a
        # disposable per-run worktree, so file against Path(state.repo) (cf. finish).
        path = write_ticket(
            Path(state.repo),
            title,
            "\n".join(body_lines),
            workflow="auto",
            priority=3,
            source_run=state.run_id,
        )
        state.failure_ticket = str(path)
    except Exception as exc:  # noqa: BLE001 - ticket filing must never mask the failure
        typer.secho(f"warning: could not file failure ticket: {exc}", fg="yellow")


def _failing_gate_log_tail(
    state: RunState, run_dir: Path, lines: int = 40
) -> tuple[str, str] | None:
    """Return (filename, tail) of the log for the failing gate, or None if unavailable."""
    gates_dir = run_dir / "gates"
    if not gates_dir.is_dir():
        return None
    log_path: Path | None = None
    if state.gate_results:
        last = state.gate_results[-1]
        attempt = last.get("attempt")
        results = cast("list[dict[str, object]]", last.get("results", []))
        for result in results:
            if not result.get("ok"):
                candidate = gates_dir / f"attempt-{attempt}-{result.get('name')}.log"
                if candidate.is_file():
                    log_path = candidate
                break
    if log_path is None:  # non-gate failure, or the computed path is missing
        logs = sorted(gates_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
        if not logs:
            return None
        log_path = logs[-1]
    try:
        text = log_path.read_text()
    except OSError:
        return None
    return log_path.name, "\n".join(text.splitlines()[-lines:])


def check_budget(ctx: WorkflowContext) -> RunOutcome | None:
    """Pause with pending_gate='budget' when total cost exceeds limits.max_cost_usd."""
    limit = ctx.config.limits.max_cost_usd
    state = ctx.state
    if limit is None or state.budget_waived or state.total_cost_usd <= limit:
        return None
    state.status = "paused"
    state.pending_gate = "budget"
    save_state(state, ctx.run_dir)
    notify(state, ctx.config)
    return RunOutcome(
        "paused",
        f"cost ${state.total_cost_usd:.2f} exceeds budget ${limit:.2f}",
        hints=[f"adw resume {state.run_id} --approve   (lift budget for this run; or --reject)"],
    )


def _resolve_budget_gate(ctx: WorkflowContext) -> RunOutcome | None:
    """Consume the engineer's decision on a pending budget gate (runs before any agent)."""
    state = ctx.state
    if state.pending_gate != "budget":
        return None
    decision, ctx.decision = ctx.decision, None
    if decision == "approve":
        state.budget_waived = True
        state.pending_gate = None
        state.status = "running"
        save_state(state, ctx.run_dir)
        return None
    if decision == "reject":
        state.status = "rejected"
        state.pending_gate = None
        state.outcome_detail = (
            f"budget exceeded (${state.total_cost_usd:.2f}); rejected — "
            f"branch {state.work_branch} kept"
        )
        save_state(state, ctx.run_dir)
        return RunOutcome(
            "rejected",
            "budget exceeded; stopped by engineer",
            hints=[f"work preserved on branch {state.work_branch}"],
        )
    return RunOutcome(
        "paused",
        "awaiting budget approval",
        hints=[f"adw resume {state.run_id} --approve   (or --reject)"],
    )


def preflight(ctx: WorkflowContext, *, require_clean: bool = True) -> RunOutcome | None:
    if (outcome := _resolve_budget_gate(ctx)) is not None:
        return outcome
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


def _discard_work(ctx: WorkflowContext) -> None:
    """Throw away the work branch (and worktree) — used when the plan is rejected."""
    state = ctx.state
    if state.worktree:
        main = Path(state.repo)
        git_ops.remove_worktree(main, Path(state.worktree))
        git_ops.delete_branch(main, state.work_branch)
    else:
        git_ops.checkout(ctx.repo_dir, state.base_branch)
        git_ops.delete_branch(ctx.repo_dir, state.work_branch)


def start_branch(ctx: WorkflowContext) -> None:
    """Create the work branch (and, under worktree isolation, a dedicated worktree).

    Repoints ctx.repo_dir to the worktree so every later step operates in isolation.
    """
    state = ctx.state
    if _done(ctx, "branch"):
        if state.worktree:  # resuming: operate in the existing worktree
            ctx.repo_dir = Path(state.worktree)
        else:
            git_ops.checkout(ctx.repo_dir, state.work_branch)
        return
    state.base_branch = git_ops.current_branch(ctx.repo_dir)
    state.work_branch = f"{ctx.config.ship.branch_prefix}{state.run_id}"
    state.start_step("branch")
    # worktree AND container isolation both get a per-run worktree so git state is
    # isolated (parallel-safe); container additionally runs agents/gates in a VM
    # that mounts that worktree.
    if ctx.config.isolation.type in ("worktree", "container"):
        worktree = ctx.repo_dir / ctx.config.isolation.worktrees_dir / state.run_id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git_ops.add_worktree(ctx.repo_dir, worktree, state.work_branch, state.base_branch)
        state.worktree = str(worktree)
        ctx.repo_dir = worktree
    else:
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
    return check_budget(ctx)


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
        notify(state, ctx.config)
        return RunOutcome(
            "paused",
            "awaiting plan approval",
            hints=[
                f"review {ctx.run_dir / artifact_name}",
                f"adw resume {state.run_id} --approve   (or --reject)",
            ],
        )
    if decision != "approve":
        _discard_work(ctx)
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
    return check_budget(ctx)


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
    return check_budget(ctx)


def gate_loop(
    ctx: WorkflowContext, *, role: str = "build", step_prefix: str = ""
) -> RunOutcome | None:
    """Run all gates; on failure, resume the build session with the failures. Repeat."""
    state, config, repo = ctx.state, ctx.config, ctx.repo_dir
    if any(r.name.startswith(f"{step_prefix}gates-") and r.status == "ok" for r in state.steps):
        return None  # this round's gates already passed (resume)
    gate_order = config.gate_order()
    gates_dir = ctx.run_dir / "gates"
    max_fixes = config.workflow.max_fix_iterations
    passed = False
    for attempt in range(1, max_fixes + 2):
        state.start_step(f"{step_prefix}gates-{attempt}")
        results = code_node.run_gates(gate_order, config.gates, repo, gates_dir, attempt, ctx.env)
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
            state.end_step(f"{step_prefix}gates-{attempt}", "ok")
            state.gates_passed = True
            save_state(state, ctx.run_dir)
            passed = True
            break
        names = ", ".join(f.name for f in failures)
        state.end_step(f"{step_prefix}gates-{attempt}", "failed", f"failed: {names}")
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
            step_name=f"{step_prefix}fix-{attempt}",
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


def _parse_verdict(text: str) -> str:
    """Extract the review verdict. Unparseable/empty -> 'ship' (never loops)."""
    for line in text.splitlines():
        match = re.match(r"\s*VERDICT:\s*(ship|concerns)\b", line, re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return "ship"


_WORKFLOW_COMMIT_TYPE = {
    "feature": "feat",
    "bug": "fix",
    "chore": "chore",
    "hotfix": "fix",
    "cve": "fix",
}


def _commit_subject(task: str, workflow: str, max_len: int = 72) -> str:
    """Deterministic conventional-commit subject: '<type>: <first line, truncated>'."""
    first_line = task.strip().splitlines()[0] if task.strip() else ""
    subject = first_line[:max_len].rstrip()
    prefix = _WORKFLOW_COMMIT_TYPE.get(workflow, "chore")
    return f"{prefix}: {subject}"


def review(
    ctx: WorkflowContext,
    *,
    context: str,
    prompt_name: str = "review",
    step_name: str = "review",
) -> tuple[str, str]:
    """Fresh-session, read-only review of the diff. Returns (verdict, review_text)."""
    repo = ctx.repo_dir
    state = ctx.state
    if _done(ctx, step_name):
        # resuming: re-parse the latest review.md (skipped reviews left no file).
        review_path = ctx.run_dir / "review.md"
        if review_path.exists():
            text = review_path.read_text()
            return _parse_verdict(text), text
        return "ship", ""
    git_ops.stage_all(repo)  # so new files appear in the diff vs the base
    state.start_step(step_name)
    result = ctx.agents.run(
        "review",
        prompts.render(
            prompt_name,
            context=context,
            diff=code_node.truncate_middle(git_ops.full_diff(repo, state.base_branch)),
        ),
        cwd=repo,
        step_name=step_name,
        read_only=True,
    )
    state.add_cost(result.cost_usd)
    if result.ok and result.output.strip():
        (ctx.run_dir / "review.md").write_text(result.output)
        state.end_step(step_name, "ok")
        save_state(state, ctx.run_dir)
        return _parse_verdict(result.output), result.output
    state.end_step(step_name, "skipped", f"review agent failed: {result.error}")
    save_state(state, ctx.run_dir)
    return "ship", ""


def review_loop(
    ctx: WorkflowContext, *, context: str, prompt_name: str = "review"
) -> RunOutcome | None:
    """Review the diff; on 'concerns', resume the build session to revise,
    re-run the gates, and re-review — up to workflow.max_review_iterations rounds."""
    state = ctx.state
    max_rounds = ctx.config.workflow.max_review_iterations
    for round_no in range(max_rounds + 1):
        step_name = "review" if round_no == 0 else f"review-{round_no + 1}"
        verdict, review_text = review(
            ctx, context=context, prompt_name=prompt_name, step_name=step_name
        )
        if (outcome := check_budget(ctx)) is not None:
            return outcome
        if verdict != "concerns" or round_no >= max_rounds:
            break
        revise = round_no + 1
        typer.secho(
            f"review raised concerns; routing back to build agent [revise {revise}/{max_rounds}]",
            fg="yellow",
        )
        state.review_rounds = revise
        outcome = resume_turn(
            ctx, prompts.render("revise", review=review_text), step_name=f"revise-{revise}"
        )
        if outcome is not None:
            return outcome
        if (outcome := gate_loop(ctx, step_prefix=f"r{revise}-")) is not None:
            return outcome
    state.status = "awaiting_final_review"
    save_state(state, ctx.run_dir)
    return None


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
        notify(state, ctx.config)
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
    subject = title or _commit_subject(ctx.task, state.workflow)
    commit = git_ops.commit_all(
        repo, f"{subject}\n\n{ctx.task}\n\nadw-run: {state.run_id}"
    )
    detail = f"commit {commit} on {state.work_branch}"

    if config.ship.create_pr:
        if not git_ops.has_remote(repo):
            detail += f"; PR skipped (no git remote configured), branch {state.work_branch}"
            typer.secho(
                "ship: create_pr is on but the repo has no git remote; skipping PR, "
                f"branch {state.work_branch} kept",
                fg="yellow",
            )
        elif not shutil.which("gh"):
            detail += f"; PR skipped (gh CLI not found), branch {state.work_branch}"
            typer.secho(
                "ship: create_pr is on but `gh` is not installed; skipping PR, "
                f"branch {state.work_branch} kept",
                fg="yellow",
            )
        else:
            try:
                git_ops.push_branch(repo, state.work_branch)
                summary = git_ops.diff_summary(repo, state.base_branch)
                pr_url = git_ops.create_pr(
                    repo, subject, f"Automated by adw run {state.run_id}\n\n{summary}"
                )
                detail += f"; PR {pr_url}"
            except git_ops.GitError as exc:
                detail += f"; PR skipped ({exc}), branch {state.work_branch}"
                typer.secho(
                    f"ship: PR creation failed ({exc}); branch {state.work_branch} kept",
                    fg="yellow",
                )

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
