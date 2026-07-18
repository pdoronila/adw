"""Read-only helpers over run artifacts for the web UI.

These mirror the read logic of `adw logs` (see `adw.cli.logs`) but return data
structures the templates render, and tolerate half-written runs (a freshly
spawned run whose `state.json` isn't on disk yet).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import markdown  # type: ignore[import-untyped]

from adw.nodes import git_ops
from adw.state import run_state as rs
from adw.workflows import WORKFLOWS

# Raw run status -> pill class suffix. Mirrors `_STATUS_COLOR` in cli.py, plus
# the awaiting_* gate states collapse onto the "paused" pill, and running=blue.
PILL_STATUS: dict[str, str] = {
    "running": "running",
    "paused": "paused",
    "awaiting_plan_approval": "paused",
    "awaiting_final_review": "paused",
    "shipped": "shipped",
    "failed": "failed",
    "rejected": "rejected",
    "cancelled": "cancelled",
}

TERMINAL = {"shipped", "failed", "rejected", "cancelled"}
PAUSED = {"paused", "awaiting_plan_approval", "awaiting_final_review"}

# Toast keys carried through `?toast=<key>` redirect params. Only known keys
# render (free text from the URL never reaches the page).
TOAST_MESSAGES: dict[str, str] = {
    "run-started": "Run started",
    "approved": "Run approved — resuming",
    "rejected": "Run rejected",
    "retry-started": "Retry started",
    "cancel-requested": "Cancel requested",
    "ticket-created": "Ticket created",
    "ticket-deleted": "Ticket deleted",
    "ticket-requeued": "Ticket requeued",
    "ticket-started": "Ticket started — it will move to In progress shortly",
    "ticket-blocked": "Ticket is blocked by unfinished tickets",
    "queue-processing": "Queue processing started",
}


def toast_message(key: str) -> str | None:
    """Message for a `?toast=` key, or None for unknown/empty keys."""
    return TOAST_MESSAGES.get(key)


def pill_class(status: str) -> str:
    """Jinja helper: raw status -> `pill-*` CSS class suffix."""
    return PILL_STATUS.get(status, "running")


def filter_runs(runs: list[rs.RunState], q: str = "", status: str = "") -> list[rs.RunState]:
    """Filter runs by substring (run_id/task/workflow) and status.

    `status="paused"` matches all human-gate states (mirrors `PAUSED`).
    """
    needle = q.strip().lower()
    out: list[rs.RunState] = []
    for run in runs:
        if needle and not any(
            needle in field.lower() for field in (run.run_id, run.task, run.workflow)
        ):
            continue
        if status:
            if status == "paused":
                if run.status not in PAUSED:
                    continue
            elif run.status != status:
                continue
        out.append(run)
    return out


def humanize_ts(dt: datetime) -> str:
    """Jinja helper: aware datetime -> 'just now' / '4m ago' / '3h ago' / date."""
    delta = datetime.now(UTC) - dt
    seconds = delta.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)}d ago"
    return dt.date().isoformat()


def clock_ts(dt: datetime) -> str:
    """Jinja helper: aware datetime -> 'HH:MM:SS' UTC clock time."""
    return dt.astimezone(UTC).strftime("%H:%M:%S")


def list_runs(repo: Path) -> list[rs.RunState]:
    """All runs, newest first (for the dashboard table)."""
    return list(reversed(rs.list_runs(repo)))


def dashboard_metrics(runs: list[rs.RunState]) -> dict[str, object]:
    """Aggregate health/cost/throughput metrics for the dashboard page.

    Ratios are None (not 0) when their denominator is empty so templates can
    render an em dash instead of a misleading zero.
    """
    status_counts: dict[str, int] = {status: 0 for status in get_args(rs.RunStatus)}
    for run in runs:
        status_counts[run.status] += 1
    terminal = sum(status_counts[status] for status in TERMINAL)
    shipped = status_counts["shipped"]
    rollup = rs.cost_rollup(runs)
    now = datetime.now(UTC)
    return {
        "status_counts": status_counts,
        "active": [run for run in runs if run.status == "running"],
        "attention": [run for run in runs if run.status in PAUSED],
        "terminal": terminal,
        "shipped": shipped,
        "success_rate": shipped / terminal if terminal else None,
        "rollup": rollup,
        "avg_cost": rollup.total_cost_usd / rollup.runs if rollup.runs else None,
        "runs_24h": sum(1 for run in runs if now - run.created_at <= timedelta(hours=24)),
        "runs_7d": sum(1 for run in runs if now - run.created_at <= timedelta(days=7)),
        "avg_fix_attempts": sum(r.fix_attempts for r in runs) / len(runs) if runs else None,
    }


def get_state(repo: Path, run_id: str) -> rs.RunState | None:
    """Load one run's state, or None if it isn't written/parseable yet."""
    run_dir = rs.runs_root(repo) / run_id
    if not (run_dir / "state.json").is_file():
        return None
    try:
        return rs.load_state(run_dir)
    except ValueError:
        return None


def agent_transcripts(run_dir: Path, tail: int = 4000) -> list[dict[str, object]]:
    """Parsed `agent/*.json` artifacts, output truncated; unparseable ones skipped."""
    agent_dir = run_dir / "agent"
    transcripts: list[dict[str, object]] = []
    for path in sorted(agent_dir.glob("*.json")) if agent_dir.is_dir() else []:
        try:
            artifact = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        output = (artifact.get("output") or "")[:tail]
        transcripts.append(
            {
                "name": path.stem,
                "role": artifact.get("role"),
                "model": artifact.get("model"),
                "backend": artifact.get("backend"),
                "cost_usd": artifact.get("cost_usd") or 0.0,
                "output": output,
                "ok": artifact.get("ok"),
            }
        )
    return transcripts


def gate_logs(run_dir: Path, tail: int = 4000) -> list[tuple[str, str]]:
    """Sorted `gates/*.log` name + (truncated) content."""
    gates_dir = run_dir / "gates"
    logs: list[tuple[str, str]] = []
    for path in sorted(gates_dir.glob("*.log")) if gates_dir.is_dir() else []:
        try:
            logs.append((path.name, path.read_text()[:tail]))
        except OSError:
            continue
    return logs


def render_markdown_file(run_dir: Path, name: str) -> str | None:
    """Render `plan.md`/`review.md` to HTML, or None when the file is absent."""
    path = run_dir / name
    if not path.is_file():
        return None
    text = path.read_text()
    html: str = markdown.markdown(text, extensions=["fenced_code", "tables"])
    return html


def changed_files(state: rs.RunState, patch_cap: int = 20_000) -> list[dict[str, object]]:
    """Per-file diff of base_branch..work_branch: {path, added, removed, patch}.

    Empty list when either branch is unset (run hasn't branched yet), missing
    (rejected runs delete the work branch), or the repo is gone.
    """
    repo = Path(state.repo)
    if not (
        repo.is_dir()
        and git_ops.is_git_repo(repo)
        and git_ops.branch_exists(repo, state.base_branch)
        and git_ops.branch_exists(repo, state.work_branch)
    ):
        return []
    raw = git_ops.branch_diff(repo, state.base_branch, state.work_branch)
    if not raw:
        return []

    files: list[dict[str, object]] = []
    chunk: list[str] = []

    def flush() -> None:
        if not chunk:
            return
        header = chunk[0]
        path = header.rsplit(" b/", 1)[-1].strip()
        added = sum(1 for line in chunk if line.startswith("+") and not line.startswith("+++ "))
        removed = sum(1 for line in chunk if line.startswith("-") and not line.startswith("--- "))
        patch = "\n".join(chunk)[:patch_cap]
        files.append({"path": path, "added": added, "removed": removed, "patch": patch})

    for line in raw.splitlines():
        if line.startswith("diff --git "):
            flush()
            chunk = [line]
        elif chunk:
            chunk.append(line)
    flush()
    return files


def workflow_options() -> list[tuple[str, str]]:
    """(name, description) pairs for the start-run / new-ticket workflow selects."""
    return [(name, wf.description) for name, wf in sorted(WORKFLOWS.items())]


def _sse_frame(event: str, html: str) -> str:
    """Encode an SSE frame, emitting one `data:` line per fragment line."""
    lines = "\n".join(f"data: {line}" for line in html.splitlines() or [""])
    return f"event: {event}\n{lines}\n\n"


def timeline_events(
    repo: Path,
    run_id: str,
    render: Callable[[rs.RunState], list[tuple[str, str]]],
    interval: float = 0.5,
    max_seconds: float = 3600.0,
) -> Iterator[str]:
    """Poll a run's state and yield SSE frames whenever it changes.

    Always emits the current state once on first load so the client syncs
    immediately, then yields again on each `updated_at` change. Each tick yields
    one SSE frame per `(event, html)` pair returned by the callback. Stops after
    emitting a terminal or paused status (the run won't advance without a human).
    Tolerates a not-yet-written state (sleep and retry); `max_seconds` caps it.
    """
    last_seen = None
    elapsed = 0.0
    while elapsed < max_seconds:
        state = get_state(repo, run_id)
        if state is not None and state.updated_at != last_seen:
            last_seen = state.updated_at
            for event, html in render(state):
                yield _sse_frame(event, html)
            if state.status in TERMINAL or state.status in PAUSED:
                return
        time.sleep(interval)
        elapsed += interval
