"""The two engineer touchpoints: plan approval and final review.

Everything between these two gates runs without a human.
`ADW_ASSUME_YES=1` (or the auto flags) bypasses them for tests/unattended runs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

import typer

PlanDecision = Literal["approve", "reject"]


def _assume_yes() -> bool:
    return os.environ.get("ADW_ASSUME_YES", "") == "1"


def approve_plan(plan_path: Path, *, auto: bool = False) -> PlanDecision:
    """Show the plan; engineer approves, edits in $EDITOR then re-decides, or rejects."""
    if auto or _assume_yes():
        return "approve"
    typer.secho("\n════════ PLAN (engineer gate 1/2) ════════", bold=True)
    typer.echo(plan_path.read_text())
    typer.secho("══════════════════════════════════════════", bold=True)
    while True:
        choice = typer.prompt("[y] approve / [e] edit in $EDITOR / [n] reject", default="y")
        choice = choice.strip().lower()
        if choice in ("y", "yes"):
            return "approve"
        if choice in ("n", "no"):
            return "reject"
        if choice == "e":
            editor = os.environ.get("EDITOR", "vi")
            subprocess.run([editor, str(plan_path)])
            typer.echo(plan_path.read_text())


def final_review(diff_summary: str, review_path: Path, *, auto: bool = False) -> bool:
    """Show the diff summary and the review agent's report; engineer ships or rejects."""
    if auto or _assume_yes():
        return True
    typer.secho("\n════════ FINAL REVIEW (engineer gate 2/2) ════════", bold=True)
    typer.echo(diff_summary)
    if review_path.is_file():
        typer.secho("\n──────── review agent report ────────", bold=True)
        typer.echo(review_path.read_text())
    typer.secho("══════════════════════════════════════════════════", bold=True)
    choice = typer.prompt("[y] ship / [n] reject", default="y")
    return choice.strip().lower() in ("y", "yes")
