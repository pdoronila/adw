"""The v1 ADW: plan → approve → build → gate loop → review → ship.

Explicit Python control flow — deterministic code owns the orchestration,
agents own the fuzzy work, the engineer owns the two ends.
"""

from __future__ import annotations

import typer

from adw import human, prompts
from adw.nodes import code_node, git_ops
from adw.nodes.code_node import GateResult
from adw.state.run_state import save_state
from adw.workflows.base import RunOutcome, WorkflowContext


class FeatureWorkflow:
    name = "feature"
    description = "plan -> engineer approval -> build -> gate loop -> review -> engineer ship"

    def run(self, ctx: WorkflowContext) -> RunOutcome:
        state, config, repo = ctx.state, ctx.config, ctx.repo_dir

        # 0. Preflight (code): fail fast before any tokens are spent.
        problems = self._preflight(ctx)
        if problems:
            return self._fail(ctx, "preflight", "; ".join(problems))

        # 1. Branch (code).
        state.base_branch = git_ops.current_branch(repo)
        state.work_branch = f"{config.ship.branch_prefix}{state.run_id}"
        state.start_step("branch")
        git_ops.create_branch(repo, state.work_branch)
        state.end_step("branch", "ok", state.work_branch)
        save_state(state, ctx.run_dir)

        # 2. PLAN (agent, read-only). The plan text is the agent's final message;
        #    the harness writes plan.md — deterministic and backend-agnostic.
        state.start_step("plan")
        plan_result = ctx.agents.run(
            "plan",
            prompts.render("plan", task=ctx.task),
            cwd=repo,
            step_name="plan",
            read_only=True,
        )
        state.add_cost(plan_result.cost_usd)
        if not plan_result.ok or not plan_result.output.strip():
            state.end_step("plan", "failed", plan_result.error)
            return self._fail(ctx, "plan", f"plan agent failed: {plan_result.error}")
        plan_path = ctx.run_dir / "plan.md"
        plan_path.write_text(plan_result.output)
        state.end_step("plan", "ok", session_id_note(plan_result.session_id))
        state.status = "awaiting_plan_approval"
        save_state(state, ctx.run_dir)

        # 3. HUMAN GATE 1: approve / edit / reject.
        decision = human.approve_plan(plan_path, auto=ctx.auto_approve_plan or ctx.assume_yes)
        if decision != "approve":
            git_ops.checkout(repo, state.base_branch)
            git_ops.delete_branch(repo, state.work_branch)
            state.status = "rejected"
            state.outcome_detail = "plan rejected by engineer"
            save_state(state, ctx.run_dir)
            return RunOutcome("rejected", "plan rejected by engineer")
        state.status = "running"
        save_state(state, ctx.run_dir)

        # 4. BUILD (agent, write access). Its session is THE session the fix loop resumes.
        state.start_step("build")
        build_result = ctx.agents.run(
            "build",
            prompts.render("build", plan=plan_path.read_text()),
            cwd=repo,
            step_name="build",
        )
        state.add_cost(build_result.cost_usd)
        state.build_session_id = build_result.session_id
        if not build_result.ok:
            state.end_step("build", "failed", build_result.error)
            return self._fail(ctx, "build", f"build agent failed: {build_result.error}")
        state.end_step("build", "ok", session_id_note(build_result.session_id))
        save_state(state, ctx.run_dir)

        # 5. GATE LOOP (code): run all gates; on failure, resume the SAME build session.
        #    Gates run once after the build, then once after each fix round —
        #    up to max_fix_iterations fix rounds.
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
                save_state(state, ctx.run_dir)
                passed = True
                break
            names = ", ".join(f.name for f in failures)
            state.end_step(f"gates-{attempt}", "failed", f"failed: {names}")
            save_state(state, ctx.run_dir)
            if attempt > max_fixes:
                break
            typer.secho(
                f"gates failed ({names}); routing back to build agent "
                f"[fix {attempt}/{max_fixes}]",
                fg="yellow",
            )
            state.fix_attempts = attempt
            state.start_step(f"fix-{attempt}")
            fix_result = ctx.agents.run(
                "build",
                prompts.render("fix", failures=render_failures(failures)),
                cwd=repo,
                step_name=f"fix-{attempt}",
                session_id=state.build_session_id,
            )
            state.add_cost(fix_result.cost_usd)
            if not fix_result.ok:
                state.end_step(f"fix-{attempt}", "failed", fix_result.error)
                return self._fail(ctx, "fix", f"fix agent failed: {fix_result.error}")
            # Some backends mint a new session id on resume; track the latest.
            if fix_result.session_id:
                state.build_session_id = fix_result.session_id
            state.end_step(f"fix-{attempt}", "ok")
            save_state(state, ctx.run_dir)
        if not passed:
            return self._fail(
                ctx,
                "gates",
                f"gates still failing after {config.workflow.max_fix_iterations} fix attempts",
                hints=[f"inspect logs: {gates_dir}", f"adw status {state.run_id}"],
            )

        # 6. REVIEW (agent, FRESH session, read-only) — advisory in v1.
        git_ops.stage_all(repo)  # so new files appear in diffs vs the base
        state.start_step("review")
        review_result = ctx.agents.run(
            "review",
            prompts.render(
                "review",
                plan=plan_path.read_text(),
                diff=code_node.truncate_middle(git_ops.full_diff(repo, state.base_branch)),
            ),
            cwd=repo,
            step_name="review",
            read_only=True,
        )
        state.add_cost(review_result.cost_usd)
        review_path = ctx.run_dir / "review.md"
        if review_result.ok and review_result.output.strip():
            review_path.write_text(review_result.output)
            state.end_step("review", "ok")
        else:
            state.end_step("review", "skipped", f"review agent failed: {review_result.error}")
        state.status = "awaiting_final_review"
        save_state(state, ctx.run_dir)

        # 7. HUMAN GATE 2: ship or reject (reject keeps the branch for salvage).
        summary = git_ops.diff_summary(repo, state.base_branch)
        if not human.final_review(summary, review_path, auto=ctx.assume_yes):
            state.status = "rejected"
            state.outcome_detail = f"rejected at final review; branch {state.work_branch} kept"
            save_state(state, ctx.run_dir)
            return RunOutcome(
                "rejected",
                "rejected at final review",
                hints=[f"work preserved on branch {state.work_branch}"],
            )

        # 8. SHIP (code).
        state.start_step("ship")
        commit = git_ops.commit_all(repo, f"{ctx.task}\n\nadw-run: {state.run_id}")
        detail = f"commit {commit} on {state.work_branch}"
        if config.ship.create_pr:
            pr_url = git_ops.create_pr(
                repo, ctx.task, f"Automated by adw run {state.run_id}\n\n{summary}"
            )
            detail += f"; PR {pr_url}"
        state.end_step("ship", "ok", detail)
        state.status = "shipped"
        state.outcome_detail = detail
        save_state(state, ctx.run_dir)
        return RunOutcome("shipped", detail)

    def _preflight(self, ctx: WorkflowContext) -> list[str]:
        problems = []
        if not git_ops.is_git_repo(ctx.repo_dir):
            problems.append(f"{ctx.repo_dir} is not a git repository")
        elif not git_ops.ensure_clean(ctx.repo_dir):
            problems.append("working tree is not clean; commit or stash first")
        if not ctx.config.gates:
            problems.append("no gates configured in adw.yaml")
        else:
            missing = [g for g in ctx.config.gate_order() if g not in ctx.config.gates]
            if missing:
                problems.append(f"gate_order names undefined gates: {missing}")
        return problems

    def _fail(
        self,
        ctx: WorkflowContext,
        step: str,
        reason: str,
        hints: list[str] | None = None,
    ) -> RunOutcome:
        ctx.state.status = "failed"
        ctx.state.outcome_detail = f"{step}: {reason}"
        save_state(ctx.state, ctx.run_dir)
        return RunOutcome("failed", reason, hints=hints or [])


def render_failures(failures: list[GateResult]) -> str:
    blocks = []
    for f in failures:
        blocks.append(
            f"## Gate `{f.name}` failed (exit {f.exit_code})\n"
            f"Command: `{f.command}`\n\n"
            f"```\n{f.output_excerpt}\n```"
        )
    return "\n\n".join(blocks)


def session_id_note(session_id: str | None) -> str:
    return f"session {session_id}" if session_id else ""
