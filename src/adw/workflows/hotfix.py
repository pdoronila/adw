"""The hotfix ADW: surgical, fast, human-approved. For production-down crises.

Scout the fault, propose the smallest safe fix, get engineer sign-off on the
approach (human-in-the-loop — this is going straight to prod), implement, gate,
ship ASAP. No slow review agent; the human approves the solution up front.
"""

from __future__ import annotations

from adw import prompts
from adw.workflows import steps
from adw.workflows.base import RunOutcome, WorkflowContext


class HotfixWorkflow:
    name = "hotfix"
    description = "scout -> engineer approves fix -> implement -> gate loop -> ship ASAP"

    def run(self, ctx: WorkflowContext) -> RunOutcome:
        if (outcome := steps.preflight(ctx)) is not None:
            return outcome
        steps.start_branch(ctx)

        # SCOUT (read-only) -> engineer approves the surgical fix before it is built.
        if (outcome := steps.agent_doc(
            ctx,
            role="plan",
            step_name="scout",
            prompt_text=prompts.render("hotfix_scout", task=ctx.task),
            out_name="plan.md",
        )) is not None:
            return outcome
        if (outcome := steps.approve_gate(
            ctx, "plan.md", reject_reason="hotfix approach rejected by engineer"
        )) is not None:
            return outcome
        fix_plan = (ctx.run_dir / "plan.md").read_text()

        # IMPLEMENT -> gate loop.
        build_prompt = prompts.render("hotfix_build", fix_plan=fix_plan)
        if (outcome := steps.build(ctx, build_prompt)) is not None:
            return outcome
        if (outcome := steps.gate_loop(ctx)) is not None:
            return outcome

        # Final confirmation, then ship. (Human already approved the approach.)
        if (outcome := steps.final_gate(ctx)) is not None:
            return outcome
        return steps.ship(ctx)
