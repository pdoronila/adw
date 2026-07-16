"""The CVE ADW: reproduce a vulnerability, then build the protection against it.

Defensive security workflow. Given a CVE (id and/or description) that may affect
this codebase:

  research  -> engineer approves scope (read-only analysis)
  reproduce -> a security/regression test that FAILS against the vulnerable code
  mitigate  -> implement the protection (resumes the reproduce session)
  gate loop -> the whole suite, including the new security test, must PASS
               (green = the protection closes the hole and nothing else broke)
  review    -> security-focused review
  ship

The reproduction test is the durable artifact: it stays in the suite as a
regression guard so the vulnerability can never silently return.
"""

from __future__ import annotations

from adw import prompts
from adw.workflows import steps
from adw.workflows.base import RunOutcome, WorkflowContext


class CveWorkflow:
    name = "cve"
    description = "research -> approve -> reproduce (failing test) -> mitigate -> gate loop -> ship"

    def run(self, ctx: WorkflowContext) -> RunOutcome:
        if (outcome := steps.preflight(ctx)) is not None:
            return outcome
        steps.start_branch(ctx)

        # RESEARCH (read-only) -> engineer approves scope before any code is written.
        if (outcome := steps.agent_doc(
            ctx,
            role="plan",
            step_name="research",
            prompt_text=prompts.render("cve_research", task=ctx.task),
            out_name="plan.md",
        )) is not None:
            return outcome
        if (outcome := steps.approve_gate(
            ctx, "plan.md", reject_reason="CVE analysis rejected by engineer"
        )) is not None:
            return outcome
        analysis = (ctx.run_dir / "plan.md").read_text()

        # REPRODUCE: write a security test that fails against the vulnerable code.
        # This opens THE session the mitigation and gate loop resume.
        if (outcome := steps.build(
            ctx,
            prompts.render("cve_reproduce", analysis=analysis),
            step_name="reproduce",
        )) is not None:
            return outcome

        # MITIGATE: same session now implements the protection.
        if (outcome := steps.resume_turn(
            ctx,
            prompts.render("cve_mitigate", analysis=analysis),
            step_name="mitigate",
        )) is not None:
            return outcome

        # Gate loop: green means the security test passes (hole closed) and the
        # rest of the suite still passes (no regressions).
        if (outcome := steps.gate_loop(ctx)) is not None:
            return outcome

        # REVIEW loop: concerns route back to the build session, then re-gate + re-review.
        if (outcome := steps.review_loop(
            ctx, context=analysis, prompt_name="cve_review"
        )) is not None:
            return outcome
        if (outcome := steps.final_gate(ctx)) is not None:
            return outcome
        return steps.ship(ctx)
