"""Run state: one directory per run under the repo's runs root.

The runs root is two-tier (see runs_root): repos with an existing
<repo>/.adw/runs/ keep using it (legacy tier); everything else lands in
~/.adw/<repo-slug>/runs/ (user tier).

state.json is rewritten atomically at every step boundary — it is the
observability substrate and the hook for a future `adw resume`.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from adw import config, registry
from adw.adapters.base import AgentResult, TokenUsage

RunStatus = Literal[
    "running",
    "awaiting_plan_approval",
    "awaiting_final_review",
    "paused",
    "shipped",
    "failed",
    "rejected",
    "cancelled",
]
StepStatus = Literal["pending", "running", "ok", "failed", "skipped"]


def _now() -> datetime:
    return datetime.now(UTC)


class StepRecord(BaseModel):
    name: str
    status: StepStatus = "pending"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    session_id: str | None = None
    detail: str = ""
    # This step's invocation usage; None distinguishes "unreported" from zero.
    tokens: TokenUsage | None = None


class RunState(BaseModel):
    run_id: str
    workflow: str
    task: str
    repo: str
    status: RunStatus = "running"
    base_branch: str = ""
    work_branch: str = ""
    worktree: str | None = None  # set when isolation.type == "worktree"
    pid: int | None = None    # process id of the run's CLI process
    pgid: int | None = None   # its process group (start_new_session makes it a leader)
    build_session_id: str | None = None
    fix_attempts: int = 0
    # per-role gate/validation failures this run (model router upshift input)
    role_failures: dict[str, int] = Field(default_factory=dict)
    review_rounds: int = 0
    pending_gate: str | None = None  # "plan" | "final" | "budget" when awaiting a decision
    budget_waived: bool = False  # engineer approved continuing past the budget
    gates_passed: bool = False
    steps: list[StepRecord] = Field(default_factory=list)
    gate_results: list[dict[str, object]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    total_cost_usd: float = 0.0
    # Recorded tokens across the run; defaults keep legacy state.json loading.
    total_tokens: TokenUsage = Field(default_factory=TokenUsage)
    tokens_by_model: dict[str, TokenUsage] = Field(default_factory=dict)
    outcome_detail: str = ""
    # run id from the auto-filed ticket that spawned this run (loop guard)
    source_ticket_run: str | None = None
    # path of the ticket auto-filed for this run's failure
    failure_ticket: str | None = None

    def step(self, name: str) -> StepRecord:
        for record in self.steps:
            if record.name == name:
                return record
        record = StepRecord(name=name)
        self.steps.append(record)
        return record

    def start_step(self, name: str) -> StepRecord:
        record = self.step(name)
        record.status = "running"
        record.started_at = _now()
        return record

    def end_step(self, name: str, status: StepStatus, detail: str = "") -> StepRecord:
        record = self.step(name)
        record.status = status
        record.ended_at = _now()
        if detail:
            record.detail = detail
        return record

    def add_usage(self, step_name: str, result: AgentResult) -> None:
        """Record one invocation's cost and tokens (per step, run total, per model)."""
        if result.cost_usd:
            self.total_cost_usd += result.cost_usd
        if result.tokens is None:
            return  # backend didn't report usage — never fabricate counts
        self.step(step_name).tokens = result.tokens
        self.total_tokens.add(result.tokens)
        per_model = result.model_tokens or {result.model or "unknown": result.tokens}
        for model_id, usage in per_model.items():
            self.tokens_by_model.setdefault(model_id, TokenUsage()).add(usage)


def slugify(text: str, max_len: int = 24) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def new_run_id(task: str, now: datetime | None = None) -> str:
    stamp = (now or _now()).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{slugify(task)}"


def runs_root(repo: Path) -> Path:
    """Runs root for a repo: legacy <repo>/.adw/runs if it exists, else
    ~/.adw/<repo-slug>/runs. ADW_DATA_TIER=project|user forces a tier."""
    local = repo / ".adw" / "runs"
    tier = os.environ.get("ADW_DATA_TIER", "")
    if tier == "project" or (tier != "user" and local.is_dir()):
        return local
    return config.data_home() / registry.repo_slug(repo) / "runs"


def create_run_dir(repo: Path, run_id: str) -> Path:
    run_dir = runs_root(repo) / run_id
    (run_dir / "agent").mkdir(parents=True, exist_ok=True)
    (run_dir / "gates").mkdir(parents=True, exist_ok=True)
    return run_dir


def save_state(state: RunState, run_dir: Path) -> None:
    state.updated_at = _now()
    payload = state.model_dump_json(indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=run_dir, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.replace(tmp_path, run_dir / "state.json")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def load_state(run_dir: Path) -> RunState:
    return RunState.model_validate_json((run_dir / "state.json").read_text())


def list_runs(repo: Path) -> list[RunState]:
    root = runs_root(repo)
    if not root.is_dir():
        return []
    states = []
    for run_dir in sorted(root.iterdir()):
        if (run_dir / "state.json").is_file():
            try:
                states.append(load_state(run_dir))
            except ValueError:
                continue
    return states


class WorkflowCost(BaseModel):
    runs: int = 0
    total_cost_usd: float = 0.0


class CostRollup(BaseModel):
    runs: int = 0
    total_cost_usd: float = 0.0
    workflows: dict[str, WorkflowCost] = Field(default_factory=dict)


def cost_rollup(states: list[RunState]) -> CostRollup:
    """Aggregate spend across runs: grand totals plus per-workflow breakdowns."""
    rollup = CostRollup()
    for state in states:
        rollup.runs += 1
        rollup.total_cost_usd += state.total_cost_usd
        wc = rollup.workflows.setdefault(state.workflow, WorkflowCost())
        wc.runs += 1
        wc.total_cost_usd += state.total_cost_usd
    return rollup
