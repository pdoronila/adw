"""Tests for the local `adw ui` web dashboard."""

from __future__ import annotations

import json
import re
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
    state_repo: Path | None = None,
    base_branch: str = "",
    work_branch: str = "",
) -> Path:
    run_dir = create_run_dir(repo, run_id)
    state = RunState(
        run_id=run_id, workflow="feature", task="add widget", repo=str(state_repo or repo)
    )
    state.base_branch = base_branch
    state.work_branch = work_branch
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


SCRIPT_LINE = "# <script>alert(1)</script>"


def _build_feat_branch(target_repo: Path, work_branch: str = "feat") -> None:
    """Add a `feat` branch modifying app.py by +2/−1 lines (incl. an HTML line)."""
    subprocess.run(
        ["git", "checkout", "-b", work_branch], cwd=target_repo, check=True, capture_output=True
    )
    (target_repo / "app.py").write_text(f"def hello():\n    {SCRIPT_LINE}\n    return 'howdy'\n")
    subprocess.run(
        ["git", "commit", "-am", "change greeting"],
        cwd=target_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=target_repo, check=True, capture_output=True)


def test_changed_files_unit(target_repo: Path) -> None:
    _build_feat_branch(target_repo)
    state = RunState(run_id="r1", workflow="feature", task="t", repo=str(target_repo))
    state.base_branch = "main"
    state.work_branch = "feat"

    files = views.changed_files(state)
    assert len(files) == 1
    assert files[0]["path"] == "app.py"
    assert files[0]["added"] == 2
    assert files[0]["removed"] == 1
    assert "diff --git" in files[0]["patch"]  # type: ignore[operator]

    # No diff when the run hasn't branched yet, or the branch doesn't exist.
    state.work_branch = ""
    assert views.changed_files(state) == []
    state.work_branch = "nope"
    assert views.changed_files(state) == []


def test_run_detail_shows_changes_card(target_repo: Path) -> None:
    _build_feat_branch(target_repo)
    _seed_run(
        target_repo,
        "r1",
        state_repo=target_repo,
        base_branch="main",
        work_branch="feat",
    )

    resp = TestClient(create_app(target_repo)).get("/runs/r1")
    assert resp.status_code == 200
    body = resp.text
    assert "Changes" in body
    assert "app.py" in body
    assert "+2" in body
    assert "−1" in body
    # raw diff text is HTML-escaped (no |safe): the literal tag never appears
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_run_detail_no_changes_card_without_branch(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    resp = TestClient(create_app(tmp_path)).get("/runs/r1")
    assert resp.status_code == 200
    assert "<h3>Changes" not in resp.text


def test_runs_page_lists_runs(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")

    client = TestClient(create_app(tmp_path))
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "r1" in body
    assert "feature" in body


def test_tickets_page_lists_tickets(tmp_path: Path) -> None:
    ticket_mod.write_ticket(tmp_path, "Fix login", "details")

    client = TestClient(create_app(tmp_path))
    resp = client.get("/tickets")
    assert resp.status_code == 200
    assert "Fix login" in resp.text


def test_modals_present_on_pages(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    client = TestClient(create_app(tmp_path))

    for path in ("/", "/tickets", "/runs/r1"):
        body = client.get(path).text
        assert 'id="start-run-modal"' in body
        assert 'id="new-ticket-modal"' in body
        assert 'action="/runs"' in body
        assert 'action="/tickets"' in body


def test_tickets_page_nav_and_active(tmp_path: Path) -> None:
    resp = TestClient(create_app(tmp_path)).get("/tickets")
    assert resp.status_code == 200
    assert 'hx-get="/fragments/board"' in resp.text


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
    _seed_run(tmp_path, "live", status="running")

    client = TestClient(create_app(tmp_path))
    paused_body = client.get("/runs/paused").text
    assert "Approve" in paused_body
    assert "Reject" in paused_body

    failed_body = client.get("/runs/broke").text
    assert "Retry" in failed_body
    assert "Cancel" not in failed_body

    live_body = client.get("/runs/live").text
    assert "Cancel" in live_body


def test_post_tickets_creates_ticket(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), follow_redirects=False)
    resp = client.post(
        "/tickets",
        data={"title": "New task", "body": "body text", "workflow": "bug", "priority": "3"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/tickets?toast=ticket-created"
    titles = [t.title for t in ticket_mod.list_tickets(tmp_path, "queue")]
    assert "New task" in titles


def test_post_ticket_delete(tmp_path: Path) -> None:
    ticket_mod.write_ticket(tmp_path, "Delete me", "body")
    stem = ticket_mod.list_tickets(tmp_path, "queue")[0].path.stem
    client = TestClient(create_app(tmp_path), follow_redirects=False)

    resp = client.post(f"/tickets/{stem}/delete")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/tickets?toast=ticket-deleted"
    assert ticket_mod.list_tickets(tmp_path, "queue") == []


def test_post_ticket_requeue(tmp_path: Path) -> None:
    ticket_mod.write_ticket(tmp_path, "Requeue me", "body")
    ticket = ticket_mod.claim_next(tmp_path)
    assert ticket is not None
    ticket_mod.finish(ticket, tmp_path, "failed", "boom", "run-1")
    stem = ticket_mod.list_tickets(tmp_path, "failed")[0].path.stem
    client = TestClient(create_app(tmp_path), follow_redirects=False)

    resp = client.post(f"/tickets/{stem}/requeue")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/tickets?toast=ticket-requeued"
    assert ticket_mod.list_tickets(tmp_path, "failed") == []
    assert any(t.title == "Requeue me" for t in ticket_mod.list_tickets(tmp_path, "queue"))


def test_post_ticket_delete_unknown_404(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), follow_redirects=False)
    assert client.post("/tickets/nope/delete").status_code == 404
    assert client.post("/tickets/nope/requeue").status_code == 404


def test_board_renders_action_forms(tmp_path: Path) -> None:
    # Seed one failed ticket first, then leave one in the queue.
    ticket_mod.write_ticket(tmp_path, "doomed one", "")
    failed = ticket_mod.claim_next(tmp_path)
    assert failed is not None
    ticket_mod.finish(failed, tmp_path, "failed", "boom", "run-1")
    ticket_mod.write_ticket(tmp_path, "queued one", "")

    queued_stem = ticket_mod.list_tickets(tmp_path, "queue")[0].path.stem
    failed_stem = ticket_mod.list_tickets(tmp_path, "failed")[0].path.stem

    client = TestClient(create_app(tmp_path))
    body = client.get("/fragments/board").text
    assert f"/tickets/{queued_stem}/delete" in body
    assert f"/tickets/{failed_stem}/delete" in body
    assert f"/tickets/{failed_stem}/requeue" in body
    assert body.count("/requeue") == 1
    assert "hx-" not in body


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
    assert resp.headers["location"] == f"/runs/{run_id}?toast=run-started"


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


def test_post_cancel_spawns_and_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_fake_popen(monkeypatch)
    client = TestClient(create_app(tmp_path), follow_redirects=False)

    resp = client.post("/runs/r1/cancel")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/runs/r1?toast=cancel-requested"
    cancel_argv = captured[-1]
    assert "cancel" in cancel_argv
    assert "r1" in cancel_argv


def test_timeline_events_yields_and_stops(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path, "r1", status="running")
    gen = views.timeline_events(
        tmp_path, "r1", render=lambda s: [("timeline", f"steps:{len(s.steps)}")], interval=0.01
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
    gen = views.timeline_events(tmp_path, "r1", render=lambda s: [("timeline", "x")], interval=0.01)
    assert next(gen)  # emits current timeline once
    with pytest.raises(StopIteration):
        next(gen)  # then stops because status is paused


def test_run_events_emits_timeline_and_runhead(tmp_path: Path) -> None:
    _seed_run(tmp_path, "paused", status="awaiting_plan_approval", pending_gate="plan")
    client = TestClient(create_app(tmp_path))
    resp = client.get("/runs/paused/events")

    assert "event: timeline" in resp.text
    assert "event: runhead" in resp.text
    assert "step-time" in resp.text  # clock_ts helper resolves on the SSE render path

    frames = resp.text.split("\n\n")
    runhead = next(f for f in frames if f.startswith("event: runhead"))
    assert "Approve" in runhead
    assert "Reject" in runhead


def test_run_detail_shows_step_timestamps(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    state = views.get_state(tmp_path, "r1")
    assert state is not None
    plan = state.step("plan")
    assert plan.started_at is not None and plan.ended_at is not None

    body = TestClient(create_app(tmp_path)).get("/runs/r1").text
    assert "step-time" in body
    assert views.clock_ts(plan.started_at) in body
    assert views.clock_ts(plan.ended_at) in body


def test_timeline_pending_step_has_no_timestamp(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path, "r1")
    state = views.get_state(tmp_path, "r1")
    assert state is not None
    state.step("build")  # pending step: no start_step, so started_at is None
    save_state(state, run_dir)

    resp = TestClient(create_app(tmp_path)).get("/runs/r1")
    assert resp.status_code == 200
    assert "build" in resp.text  # pending step renders without crashing
    assert resp.text.count("step-time") == 1  # only the completed plan step


def test_filter_runs_unit(tmp_path: Path) -> None:
    _seed_run(tmp_path, "alpha", status="shipped")
    _seed_run(tmp_path, "beta", status="awaiting_plan_approval", pending_gate="plan")
    _seed_run(tmp_path, "gamma", status="failed")
    runs = views.list_runs(tmp_path)

    assert {r.run_id for r in views.filter_runs(runs)} == {"alpha", "beta", "gamma"}
    assert [r.run_id for r in views.filter_runs(runs, q="ALPH")] == ["alpha"]
    assert [r.run_id for r in views.filter_runs(runs, q="widget", status="failed")] == ["gamma"]
    # "paused" matches the awaiting_* human-gate states too
    assert [r.run_id for r in views.filter_runs(runs, status="paused")] == ["beta"]
    assert views.filter_runs(runs, q="no-such-run") == []


def test_fragments_runs_filters(tmp_path: Path) -> None:
    _seed_run(tmp_path, "alpha", status="shipped")
    _seed_run(tmp_path, "gamma", status="failed")
    client = TestClient(create_app(tmp_path))

    body = client.get("/fragments/runs").text
    assert "alpha" in body and "gamma" in body

    body = client.get("/fragments/runs", params={"q": "alpha"}).text
    assert "alpha" in body and "gamma" not in body

    body = client.get("/fragments/runs", params={"status": "failed"}).text
    assert "gamma" in body and "alpha" not in body

    body = client.get("/fragments/runs", params={"q": "no-match"}).text
    assert "No runs match" in body


def test_fragments_board(tmp_path: Path) -> None:
    ticket_mod.write_ticket(tmp_path, "Fix login", "details")
    client = TestClient(create_app(tmp_path))
    body = client.get("/fragments/board").text
    assert "Fix login" in body
    assert "no tickets" in body  # empty-column placeholder


def test_runs_page_empty_state(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    assert "No runs yet" in client.get("/").text


def test_toast_rendering(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    body = client.get("/", params={"toast": "ticket-created"}).text
    assert "Ticket created" in body
    bogus = client.get("/", params={"toast": "bogus"}).text
    assert 'id="toast"' not in bogus
    assert views.toast_message("cancel-requested") == "Cancel requested"


def test_static_assets_served(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    for asset in ("htmx.min.js", "sse.js", "app.js", "app.css"):
        assert client.get(f"/static/{asset}").status_code == 200


def test_static_assets_cache_busted(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    html = client.get("/").text
    # every local asset must carry a content-derived version query so browsers
    # can't pair new HTML with a stale cached script
    for asset in ("app.css", "htmx.min.js", "sse.js", "app.js"):
        assert re.search(rf"/static/{re.escape(asset)}\?v=\w+", html), asset
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"


def test_humanize_ts() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    assert views.humanize_ts(now) == "just now"
    assert views.humanize_ts(now - timedelta(minutes=4)) == "4m ago"
    assert views.humanize_ts(now - timedelta(hours=3)) == "3h ago"
    assert views.humanize_ts(now - timedelta(days=2)) == "2d ago"
    old = now - timedelta(days=30)
    assert views.humanize_ts(old) == old.date().isoformat()


def test_clock_ts() -> None:
    from datetime import UTC, datetime

    dt = datetime(2026, 7, 17, 18, 44, 11, tzinfo=UTC)
    assert views.clock_ts(dt) == "18:44:11"


def test_python_m_adw_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "adw", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "adw" in result.stdout
