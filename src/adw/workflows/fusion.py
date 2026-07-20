"""The fusion ADW: N-model opinions → fuse → approval → validator gate → build → validate loop.

Multi-model fusion in adw's step model: read-only opinion agents on different
backends run in parallel, a fusion agent consolidates them into one plan, and a
validator writes the executable validation gate BEFORE the builder builds —
validation failures route back into the build session like the gate loop.
"""

from __future__ import annotations

from adw import prompts
from adw.workflows import steps
from adw.workflows.base import RunOutcome, WorkflowContext


class FusionWorkflow:
    name = "fusion"
    description = (
        "N-model opinions -> fuse -> approval -> validator gate -> build -> "
        "validate loop -> review -> engineer ship"
    )

    def run(self, ctx: WorkflowContext) -> RunOutcome:
        if (outcome := steps.preflight(ctx)) is not None:
            return outcome
        steps.start_branch(ctx)

        # OPINIONS (read-only, parallel) -> side-by-side comparison doc.
        if (outcome := steps.opinion_fanout(
            ctx, roles=ctx.config.fusion.opinions, task=ctx.task
        )) is not None:
            return outcome
        opinions = (ctx.run_dir / "opinions.md").read_text()

        # FUSION (read-only) -> one consolidated plan -> engineer approval.
        if (outcome := steps.agent_doc(
            ctx,
            role="fusion",
            step_name="fusion",
            prompt_text=prompts.render("fusion", task=ctx.task, opinions=opinions),
            out_name="fusion.md",
        )) is not None:
            return outcome
        if (outcome := steps.approve_gate(
            ctx, "fusion.md", reject_reason="fused plan rejected by engineer"
        )) is not None:
            return outcome
        fused = (ctx.run_dir / "fusion.md").read_text()

        # VALIDATOR writes the gate script BEFORE the build.
        if (outcome := steps.validate_gate(ctx, fused_plan=fused)) is not None:
            return outcome

        # BUILD -> validate loop (generated gate + configured gates, same fix loop).
        if (outcome := steps.build(ctx, prompts.render("fusion_build", plan=fused))) is not None:
            return outcome
        if (outcome := steps.validate_loop(ctx)) is not None:
            return outcome

        # REVIEW loop: concerns route back to the build session, then re-gate + re-review.
        if (outcome := steps.review_loop(ctx, context=fused)) is not None:
            return outcome
        if (outcome := steps.final_gate(ctx)) is not None:
            return outcome
        return steps.ship(ctx)
