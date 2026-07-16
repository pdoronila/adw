"""Workflow contract and registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from adw.config import AdwConfig
from adw.nodes.agent_node import AgentRunner
from adw.state.run_state import RunState


@dataclass
class WorkflowContext:
    repo_dir: Path
    run_dir: Path
    config: AdwConfig
    state: RunState
    task: str
    agents: AgentRunner
    auto_approve_plan: bool = False
    assume_yes: bool = False
    # "interactive" blocks at human gates; "async" pauses and persists instead.
    mode: Literal["interactive", "async"] = "interactive"
    # A decision injected by `adw resume` for the one pending gate.
    decision: Literal["approve", "reject"] | None = None


@dataclass
class RunOutcome:
    status: Literal["shipped", "failed", "rejected", "paused"]
    reason: str = ""
    hints: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "shipped"


class Workflow(Protocol):
    name: str
    description: str

    def run(self, ctx: WorkflowContext) -> RunOutcome: ...
