"""The feature ADW: plan → approve → build → gate loop → review → ship.

Explicit Python control flow composed from reusable steps. Deterministic code
owns the orchestration; agents own the fuzzy work; the engineer owns the two ends.
"""

from __future__ import annotations

from adw import prompts
from adw.workflows import steps
from adw.workflows.base import RunOutcome, WorkflowContext


class FeatureWorkflow:
    name = "feature"
    description = "plan -> engineer approval -> build -> gate loop -> review -> engineer ship"

    def run(self, ctx: WorkflowContext) -> RunOutcome:
        if (outcome := steps.preflight(ctx)) is not None:
            return outcome
        steps.start_branch(ctx)

        # PLAN (read-only) -> engineer approval.
        if (outcome := steps.agent_doc(
            ctx,
            role="plan",
            step_name="plan",
            prompt_text=prompts.render("plan", task=ctx.task),
            out_name="plan.md",
        )) is not None:
            return outcome
        if (outcome := steps.approve_gate(
            ctx, "plan.md", reject_reason="plan rejected by engineer"
        )) is not None:
            return outcome
        plan_text = (ctx.run_dir / "plan.md").read_text()

        # BUILD -> gate loop.
        if (outcome := steps.build(ctx, prompts.render("build", plan=plan_text))) is not None:
            return outcome
        if (outcome := steps.gate_loop(ctx)) is not None:
            return outcome

        # REVIEW (advisory) -> engineer ship.
        steps.review(ctx, context=plan_text)
        if (outcome := steps.final_gate(ctx)) is not None:
            return outcome
        return steps.ship(ctx)
