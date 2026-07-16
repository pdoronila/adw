"""Tests for the local `adw ui` web dashboard."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from adw.queue import tickets as ticket_mod  # noqa: E402
from adw.state.run_state import RunState, create_run_dir, save_state  # noqa: E402
from adw.ui import views  # noqa: E402
from adw.ui.server import create_app  # noqa: E402


def _seed_run(
    repo: Path,
    run_id: str,
    status: str = "shipped",
    pending_gate: str | None = None,
) -> Path:
    run_dir = create_run_dir(repo, run_id)
    state = RunState(run_id=run_id, workflow="feature", task="add widget", repo=str(repo))
    state.start_step("plan")
    state.end_step("plan", "ok", detail="wrote plan.md")
    state.gate_results.append(
        {"attempt": 1, "results": [{"name": "lint", "ok": True, "exit_code": 0}]}
    )
    state.add_cost(1.23)
    state.status = status  # type: ignore[assignment]
    state.pending_gate = pending_gate
    save_state(state, run_dir)

    artifact = {
        "role": "plan",
        "backend": "claude-code",
        "model": "sonnet",
        "cost_usd": 1.23,
        "output": "TRANSCRIPT-MARKER" + "x" * 1000,
        "ok": True,
    }
    (run_dir / "agent" / "01-plan.json").write_text(json.dumps(artifact))
    return run_dir


def test_dashboard_lists_runs_tickets_and_workflows(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    ticket_mod.write_ticket(tmp_path, "Fix login", "details")

    client = TestClient(create_app(tmp_path))
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "r1" in body
    assert "feature" in body
    assert "Fix login" in body


def test_run_detail_renders_artifacts(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path, "r1")
    (run_dir / "plan.md").write_text("# The Plan\n\nDo the thing.")

    client = TestClient(create_app(tmp_path))
    resp = client.get("/runs/r1")
    assert resp.status_code == 200
    body = resp.text
    assert "plan" in body
    assert "TRANSCRIPT-MARKER" in body
    assert "lint" in body
    assert "$1.23" in body
    assert "The Plan" in body

    assert client.get("/runs/nope").status_code == 404


def test_action_buttons_by_status(tmp_path: Path) -> None:
    _seed_run(tmp_path, "paused", status="awaiting_plan_approval", pending_gate="plan")
    _seed_run(tmp_path, "broke", status="failed")

    client = TestClient(create_app(tmp_path))
    paused_body = client.get("/runs/paused").text
    assert "Approve" in paused_body
    assert "Reject" in paused_body

    failed_body = client.get("/runs/broke").text
    assert "Retry" in failed_body


def test_post_tickets_creates_ticket(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), follow_redirects=False)
    resp = client.post(
        "/tickets",
        data={"title": "New task", "body": "body text", "workflow": "bug", "priority": "3"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    titles = [t.title for t in ticket_mod.list_tickets(tmp_path, "queue")]
    assert "New task" in titles


def _install_fake_popen(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    captured: list[list[str]] = []

    class _Stub:
        pass

    def fake(argv: list[str], *args: Any, **kwargs: Any) -> _Stub:
        captured.append(argv)
        return _Stub()

    monkeypatch.setattr("adw.ui.runner.subprocess.Popen", fake)
    return captured


def test_post_runs_spawns_and_redirects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_fake_popen(monkeypatch)
    client = TestClient(create_app(tmp_path), follow_redirects=False)

    resp = client.post("/runs", data={"workflow": "feature", "task": "add widget"})
    assert resp.status_code == 303
    argv = captured[-1]
    for token in ("run", "feature", "--run-id", "--async", "--repo"):
        assert token in argv
    run_id = argv[argv.index("--run-id") + 1]
    assert resp.headers["location"] == f"/runs/{run_id}"


def test_post_approve_and_retry_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_fake_popen(monkeypatch)
    client = TestClient(create_app(tmp_path), follow_redirects=False)

    client.post("/runs/r1/approve")
    approve_argv = captured[-1]
    assert "resume" in approve_argv
    assert "r1" in approve_argv
    assert "--approve" in approve_argv

    client.post("/runs/r1/retry")
    retry_argv = captured[-1]
    assert "retry" in retry_argv
    assert "r1" in retry_argv


def test_timeline_events_yields_and_stops(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path, "r1", status="running")
    gen = views.timeline_events(
        tmp_path, "r1", render=lambda s: f"steps:{len(s.steps)}", interval=0.01
    )
    first = next(gen)
    assert "steps:1" in first

    state = RunState.model_validate_json((run_dir / "state.json").read_text())
    state.status = "shipped"
    save_state(state, run_dir)

    frames = list(gen)
    assert frames  # got the post-change frame
    assert "steps:1" in frames[-1]
    with pytest.raises(StopIteration):
        next(gen)


def test_timeline_events_stops_on_paused(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1", status="awaiting_plan_approval", pending_gate="plan")
    gen = views.timeline_events(
        tmp_path, "r1", render=lambda s: "x", interval=0.01
    )
    assert next(gen)  # emits current timeline once
    with pytest.raises(StopIteration):
        next(gen)  # then stops because status is paused


def test_python_m_adw_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "adw", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "adw" in result.stdout
