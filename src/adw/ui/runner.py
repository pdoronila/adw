"""Spawn adw CLI subcommands detached, logging into the run's artifact dir.

The UI is a thin control layer: it never runs a workflow in-process (that would
block the request and tie the run's lifetime to the browser). Instead it shells
out to the same `adw` CLI the terminal uses, fully detached, and reads the run
artifacts back out through `views`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from adw.state import run_state as rs


def _adw_argv() -> list[str]:
    """The argv prefix that invokes the adw CLI (installed entrypoint or `-m`)."""
    found = shutil.which("adw")
    if found:
        return [found]
    return [sys.executable, "-m", "adw"]


def spawn(repo: Path, run_id: str, args: list[str]) -> None:
    """Launch `adw <args>` detached, appending its output to <run>/ui.log.

    The log dir is created first: for `POST /runs` the run dir may not exist yet
    (the child's `create_run_dir` is idempotent, so this can't collide).
    """
    log_dir = rs.runs_root(repo) / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = (log_dir / "ui.log").open("a")
    try:
        subprocess.Popen(
            _adw_argv() + args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()


def start_run(
    repo: Path,
    run_id: str,
    workflow: str,
    task: str,
    model: str = "",
    backend: str = "",
    isolation: str = "",
) -> None:
    args = ["run", workflow, task, "--run-id", run_id, "--async", "--repo", str(repo)]
    if model:
        args += ["--model", model]
    if backend:
        args += ["--backend", backend]
    if isolation:
        args += ["--isolation", isolation]
    spawn(repo, run_id, args)


def resume_run(repo: Path, run_id: str, decision: str) -> None:
    flag = "--approve" if decision == "approve" else "--reject"
    spawn(repo, run_id, ["resume", run_id, flag, "--repo", str(repo)])


def retry_run(repo: Path, run_id: str) -> None:
    spawn(repo, run_id, ["retry", run_id, "--repo", str(repo)])


def process_queue(repo: Path) -> None:
    spawn(repo, "queue-process", ["queue", "process", "--all", "-y", "--repo", str(repo)])
