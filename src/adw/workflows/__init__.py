"""Workflow registry — add new ADWs (chore, bug, hotfix, ...) here."""

from __future__ import annotations

from adw.workflows.base import RunOutcome, Workflow, WorkflowContext
from adw.workflows.feature import FeatureWorkflow

WORKFLOWS: dict[str, Workflow] = {
    FeatureWorkflow.name: FeatureWorkflow(),
}


def get_workflow(name: str) -> Workflow:
    workflow = WORKFLOWS.get(name)
    if workflow is None:
        valid = ", ".join(sorted(WORKFLOWS))
        raise ValueError(f"unknown workflow {name!r}; valid workflows: {valid}")
    return workflow


__all__ = ["WORKFLOWS", "RunOutcome", "Workflow", "WorkflowContext", "get_workflow"]
