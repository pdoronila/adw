"""Run state: one directory per run under <repo>/.adw/runs/<run_id>/.

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
    review_rounds: int = 0
    pending_gate: str | None = None  # "plan" | "final" when awaiting an engineer decision
    gates_passed: bool = False
    steps: list[StepRecord] = Field(default_factory=list)
    gate_results: list[dict[str, object]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    total_cost_usd: float = 0.0
    outcome_detail: str = ""

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

    def add_cost(self, cost_usd: float | None) -> None:
        if cost_usd:
            self.total_cost_usd += cost_usd


def slugify(text: str, max_len: int = 24) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def new_run_id(task: str, now: datetime | None = None) -> str:
    stamp = (now or _now()).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{slugify(task)}"


def runs_root(repo: Path) -> Path:
    return repo / ".adw" / "runs"


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
