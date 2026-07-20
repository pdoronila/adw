"""Workflow registry — the factory's menu of AI developer workflows."""

from __future__ import annotations

from adw.workflows.base import RunOutcome, Workflow, WorkflowContext
from adw.workflows.bug import BugWorkflow
from adw.workflows.chore import ChoreWorkflow
from adw.workflows.cve import CveWorkflow
from adw.workflows.feature import FeatureWorkflow
from adw.workflows.fusion import FusionWorkflow
from adw.workflows.hotfix import HotfixWorkflow

WORKFLOWS: dict[str, Workflow] = {
    wf.name: wf
    for wf in (
        FeatureWorkflow(),
        BugWorkflow(),
        ChoreWorkflow(),
        HotfixWorkflow(),
        CveWorkflow(),
        FusionWorkflow(),
    )
}


def get_workflow(name: str) -> Workflow:
    workflow = WORKFLOWS.get(name)
    if workflow is None:
        valid = ", ".join(sorted(WORKFLOWS))
        raise ValueError(f"unknown workflow {name!r}; valid workflows: {valid}")
    return workflow


__all__ = ["WORKFLOWS", "RunOutcome", "Workflow", "WorkflowContext", "get_workflow"]
