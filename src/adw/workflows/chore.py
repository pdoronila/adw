"""The chore ADW: a single workhorse agent, no plan or review agent.

For small, low-risk work (dependency bumps, renames, doc tweaks). Per the
thesis: throw one workhorse agent at it, run the gates, engineer glances, ship.
"""

from __future__ import annotations

from adw import prompts
from adw.workflows import steps
from adw.workflows.base import RunOutcome, WorkflowContext


class ChoreWorkflow:
    name = "chore"
    description = "single workhorse agent -> gate loop -> engineer ship (no plan/review agents)"

    def run(self, ctx: WorkflowContext) -> RunOutcome:
        if (outcome := steps.preflight(ctx)) is not None:
            return outcome
        steps.start_branch(ctx)

        if (outcome := steps.build(ctx, prompts.render("chore_build", task=ctx.task))) is not None:
            return outcome
        if (outcome := steps.gate_loop(ctx)) is not None:
            return outcome

        # No review agent — the engineer eyeballs the diff at the ship gate.
        if (outcome := steps.final_gate(ctx)) is not None:
            return outcome
        return steps.ship(ctx)
