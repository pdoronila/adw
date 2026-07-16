"""The bug ADW: diagnose → approve → fix (with a regression test) → review → ship.

Like feature, but the plan step is a root-cause diagnosis and the fix must ship
with a regression test that fails before the change and passes after.
"""

from __future__ import annotations

from adw import prompts
from adw.workflows import steps
from adw.workflows.base import RunOutcome, WorkflowContext


class BugWorkflow:
    name = "bug"
    description = "scout -> diagnose -> approval -> fix + test -> gates -> review -> ship"

    def run(self, ctx: WorkflowContext) -> RunOutcome:
        if (outcome := steps.preflight(ctx)) is not None:
            return outcome
        steps.start_branch(ctx)

        # SCOUT (read-only) -> reconnaissance the diagnosis builds on.
        if (outcome := steps.agent_doc(
            ctx,
            role="scout",
            step_name="scout",
            prompt_text=prompts.render("scout", task=ctx.task),
            out_name="scout.md",
        )) is not None:
            return outcome
        scout_text = (ctx.run_dir / "scout.md").read_text()

        # DIAGNOSE (read-only) -> engineer approval of root cause + fix approach.
        if (outcome := steps.agent_doc(
            ctx,
            role="plan",
            step_name="diagnose",
            prompt_text=prompts.render("bug_diagnose", task=ctx.task, scout=scout_text),
            out_name="plan.md",
        )) is not None:
            return outcome
        if (outcome := steps.approve_gate(
            ctx, "plan.md", reject_reason="diagnosis rejected by engineer"
        )) is not None:
            return outcome
        diagnosis = (ctx.run_dir / "plan.md").read_text()

        # FIX + regression test -> gate loop.
        fix_prompt = prompts.render("bug_fix", diagnosis=diagnosis)
        if (outcome := steps.build(ctx, fix_prompt)) is not None:
            return outcome
        if (outcome := steps.gate_loop(ctx)) is not None:
            return outcome

        steps.review(ctx, context=diagnosis)
        if (outcome := steps.final_gate(ctx)) is not None:
            return outcome
        return steps.ship(ctx)
