"""Best-effort notifications on gate/failure transitions. Never raises."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

import typer

from adw.config import AdwConfig
from adw.state.run_state import RunState

_STATUS_LABELS = {
    "awaiting_plan_approval": "awaiting plan approval",
    "awaiting_final_review": "awaiting final review",
    "failed": "failed",
}


def notify(state: RunState, config: AdwConfig) -> None:
    """Dispatch to enabled channels; log-and-continue on any failure."""
    nc = config.notify
    label = _STATUS_LABELS.get(state.status, state.status)
    message = f"adw: {state.run_id} {label} [{state.workflow}]"
    if nc.macos and sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification {json.dumps(message)} with title "adw"'],
                capture_output=True, text=True, timeout=5,
            )
        except Exception as exc:
            typer.secho(f"notify: macos channel failed: {exc}", fg="yellow")
    if nc.webhook:
        try:
            payload = json.dumps({
                "run_id": state.run_id,
                "status": state.status,
                "workflow": state.workflow,
                "task": state.task,
                "repo": state.repo,
            }).encode()
            req = urllib.request.Request(
                nc.webhook, data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception as exc:
            typer.secho(f"notify: webhook channel failed: {exc}", fg="yellow")
