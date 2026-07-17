"""Tests for the `adw cancel <run-id>` command."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adw import cli
from adw.cli import app
from adw.state.run_state import RunState, create_run_dir, load_state, save_state

runner = CliRunner()


def _seed_run(
    repo: Path,
    run_id: str,
    status: str = "running",
    pgid: int | None = None,
) -> Path:
    run_dir = create_run_dir(repo, run_id)
    state = RunState(run_id=run_id, workflow="feature", task="t", repo=str(repo))
    state.status = status  # type: ignore[assignment]
    state.pgid = pgid
    save_state(state, run_dir)
    return run_dir


def test_cancel_signals_group_and_marks_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _seed_run(tmp_path, "r1", status="running", pgid=54321)
    captured: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.os, "killpg", lambda pgid, sig: captured.append((pgid, sig)))

    result = runner.invoke(app, ["cancel", "r1", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert captured == [(54321, signal.SIGTERM)]
    assert load_state(run_dir).status == "cancelled"


def test_cancel_non_running_is_error(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path, "r1", status="failed")
    result = runner.invoke(app, ["cancel", "r1", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "is not running" in result.output
    assert load_state(run_dir).status == "failed"


def test_cancel_missing_run_is_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["cancel", "nope", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "no run" in result.output


def test_cancel_without_pgid_still_marks_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _seed_run(tmp_path, "r1", status="running", pgid=None)

    def _fail(pgid: int, sig: int) -> None:
        raise AssertionError("killpg should not be called when pgid is None")

    monkeypatch.setattr(cli.os, "killpg", _fail)

    result = runner.invoke(app, ["cancel", "r1", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert load_state(run_dir).status == "cancelled"


def test_cancel_process_already_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _seed_run(tmp_path, "r1", status="running", pgid=54321)

    def _gone(pgid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(cli.os, "killpg", _gone)

    result = runner.invoke(app, ["cancel", "r1", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert load_state(run_dir).status == "cancelled"
