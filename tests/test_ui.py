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
from adw.ui.server import create_app, create_root_app  # noqa: E402


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
    resp = client.get("/runs")
    assert resp.status_code == 200
    body = resp.text
    assert "r1" in body
    assert "feature" in body


def test_sidebar_shows_total_spend(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")  # seeds add_cost(1.23)

    body = TestClient(create_app(tmp_path)).get("/").text
    assert "Spend" in body
    assert "$1.23" in body


def test_tickets_page_lists_tickets(tmp_path: Path) -> None:
    ticket_mod.write_ticket(tmp_path, "Fix login", "details")

    client = TestClient(create_app(tmp_path))
    resp = client.get("/tickets")
    assert resp.status_code == 200
    assert "Fix login" in resp.text


def test_modals_present_on_pages(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    client = TestClient(create_app(tmp_path))

    for path in ("/", "/runs", "/tickets", "/runs/r1"):
        body = client.get(path).text
        assert 'id="start-run-modal"' in body
        assert 'id="new-ticket-modal"' in body
        assert 'id="ticket-detail-modal"' in body
        assert 'action="/runs"' in body
        assert 'action="/tickets"' in body


def test_new_ticket_modal_offers_best_guess(tmp_path: Path) -> None:
    body = TestClient(create_app(tmp_path)).get("/tickets").text
    assert '<option value="auto">' in body
    assert "best guess" in body
    # Only the new-ticket modal offers it; POST /runs cannot handle "auto".
    assert body.count('<option value="auto">') == 1


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
    assert 'id="plan"' in body

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
    assert 'sse-swap="plan"' in live_body
    assert 'sse-swap="plan"' not in paused_body


def test_run_events_stream_includes_plan(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path, "r1", status="awaiting_plan_approval", pending_gate="plan")
    (run_dir / "plan.md").write_text("# The Plan\n\nDo the thing.")

    resp = TestClient(create_app(tmp_path)).get("/runs/r1/events")
    assert resp.status_code == 200
    assert "event: plan" in resp.text
    assert "The Plan" in resp.text
    assert "event: timeline" in resp.text


def test_run_events_plan_frame_empty_without_plan_file(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1", status="awaiting_plan_approval", pending_gate="plan")

    resp = TestClient(create_app(tmp_path)).get("/runs/r1/events")
    assert resp.status_code == 200
    assert "event: plan" in resp.text


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


def test_post_tickets_accepts_auto_workflow(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), follow_redirects=False)
    resp = client.post(
        "/tickets",
        data={"title": "Route me", "body": "", "workflow": "auto", "priority": "5"},
    )
    assert resp.status_code == 303
    tickets = ticket_mod.list_tickets(tmp_path, "queue")
    assert [t.workflow for t in tickets if t.title == "Route me"] == ["auto"]


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


def test_post_ticket_archive(tmp_path: Path) -> None:
    ticket_mod.write_ticket(tmp_path, "Archive me", "body")
    ticket = ticket_mod.claim_next(tmp_path)
    assert ticket is not None
    ticket_mod.finish(ticket, tmp_path, "shipped", "ok", "run-1")
    stem = ticket_mod.list_tickets(tmp_path, "done")[0].path.stem
    client = TestClient(create_app(tmp_path), follow_redirects=False)

    resp = client.post(f"/tickets/{stem}/archive")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/tickets?toast=ticket-archived"
    assert ticket_mod.list_tickets(tmp_path, "done") == []
    assert any(t.title == "Archive me" for t in ticket_mod.list_tickets(tmp_path, "archived"))


def test_post_ticket_archive_unknown_404(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), follow_redirects=False)
    assert client.post("/tickets/nope/archive").status_code == 404


def test_post_ticket_requeue_from_archived(tmp_path: Path) -> None:
    ticket_mod.write_ticket(tmp_path, "Restore me", "body")
    ticket = ticket_mod.claim_next(tmp_path)
    assert ticket is not None
    ticket_mod.finish(ticket, tmp_path, "shipped", "ok", "run-1")
    ticket_mod.archive(tmp_path, ticket_mod.list_tickets(tmp_path, "done")[0])
    stem = ticket_mod.list_tickets(tmp_path, "archived")[0].path.stem
    client = TestClient(create_app(tmp_path), follow_redirects=False)

    resp = client.post(f"/tickets/{stem}/requeue")
    assert resp.status_code == 303
    assert ticket_mod.list_tickets(tmp_path, "archived") == []
    assert any(t.title == "Restore me" for t in ticket_mod.list_tickets(tmp_path, "queue"))


def test_board_renders_archive_form_on_done(tmp_path: Path) -> None:
    ticket_mod.write_ticket(tmp_path, "Done one", "")
    ticket = ticket_mod.claim_next(tmp_path)
    assert ticket is not None
    ticket_mod.finish(ticket, tmp_path, "shipped", "ok", "run-1")
    stem = ticket_mod.list_tickets(tmp_path, "done")[0].path.stem

    body = TestClient(create_app(tmp_path)).get("/fragments/board").text
    assert f'action="/tickets/{stem}/archive"' in body


def test_tickets_page_shows_archived(tmp_path: Path) -> None:
    ticket_mod.write_ticket(tmp_path, "Old shipped thing", "")
    ticket = ticket_mod.claim_next(tmp_path)
    assert ticket is not None
    ticket_mod.finish(ticket, tmp_path, "shipped", "ok", "run-1")
    ticket_mod.archive(tmp_path, ticket_mod.list_tickets(tmp_path, "done")[0])

    client = TestClient(create_app(tmp_path))
    page = client.get("/tickets").text
    assert "Old shipped thing" in page
    # archived tickets never render as a board column
    board = client.get("/fragments/board").text
    assert "Old shipped thing" not in board


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


def test_post_ticket_start_spawns_and_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_fake_popen(monkeypatch)
    stem = ticket_mod.write_ticket(tmp_path, "Start me", "").stem
    client = TestClient(create_app(tmp_path), follow_redirects=False)

    resp = client.post(f"/tickets/{stem}/start")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/tickets?toast=ticket-started"
    assert captured[-1][-6:] == ["queue", "process", stem, "-y", "--repo", str(tmp_path)]


def test_post_ticket_start_unknown_404(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path), follow_redirects=False)
    assert client.post("/tickets/nope/start").status_code == 404


def test_post_ticket_start_blocked_redirects_no_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_fake_popen(monkeypatch)
    stem = ticket_mod.write_ticket(tmp_path, "Blocked", "", blocked_by=["no-such-stem"]).stem
    client = TestClient(create_app(tmp_path), follow_redirects=False)

    resp = client.post(f"/tickets/{stem}/start")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/tickets?toast=ticket-blocked"
    assert captured == []


def test_board_queue_cards_draggable(tmp_path: Path) -> None:
    stem = ticket_mod.write_ticket(tmp_path, "Drag me", "").stem

    body = TestClient(create_app(tmp_path)).get("/fragments/board").text
    assert f'draggable="true" data-ticket-id="{stem}"' in body
    assert f"/tickets/{stem}/start" in body
    assert 'data-drop-state="in_progress"' in body
    assert "hx-" not in body


def test_board_blocked_card_not_draggable(tmp_path: Path) -> None:
    stem = ticket_mod.write_ticket(tmp_path, "Blocked", "", blocked_by=["no-such-stem"]).stem

    body = TestClient(create_app(tmp_path)).get("/fragments/board").text
    assert "draggable" not in body
    assert f"/tickets/{stem}/start" not in body


def test_board_shows_blocked_badge(tmp_path: Path) -> None:
    stem = ticket_mod.write_ticket(tmp_path, "Blocker", "").stem
    ticket_mod.write_ticket(tmp_path, "Dependent", "", blocked_by=[stem])

    body = TestClient(create_app(tmp_path)).get("/fragments/board").text
    assert "pill-blocked" in body
    # blocker stems resolve to titles for display
    assert "Blocker" in body
    assert "hx-" not in body


def test_board_blocked_badge_clears_when_blocker_done(tmp_path: Path) -> None:
    stem = ticket_mod.write_ticket(tmp_path, "Blocker", "").stem
    ticket_mod.write_ticket(tmp_path, "Dependent", "", blocked_by=[stem])
    blocker = ticket_mod.claim_next(tmp_path)
    assert blocker is not None and blocker.path.stem == stem
    ticket_mod.finish(blocker, tmp_path, "shipped", "done", "run-1")

    body = TestClient(create_app(tmp_path)).get("/fragments/board").text
    assert "pill-blocked" not in body


def test_board_blocked_badge_missing_ticket_falls_back_to_stem(tmp_path: Path) -> None:
    ticket_mod.write_ticket(tmp_path, "Dependent", "", blocked_by=["no-such-ticket"])

    body = TestClient(create_app(tmp_path)).get("/fragments/board").text
    assert "pill-blocked" in body
    assert "no-such-ticket" in body


def test_ticket_detail_fragment_shows_full_details(tmp_path: Path) -> None:
    stem = ticket_mod.write_ticket(
        tmp_path,
        "Fix the login flow",
        "line one\n\nline two much longer than any card snippet",
        workflow="feature",
        priority=3,
    ).stem

    resp = TestClient(create_app(tmp_path)).get(f"/fragments/tickets/{stem}")
    assert resp.status_code == 200
    body = resp.text
    assert "Fix the login flow" in body
    assert "line one" in body
    assert "line two much longer than any card snippet" in body
    assert "p3" in body
    assert "feature" in body
    assert "queue" in body
    assert "data-modal-close" in body


def test_ticket_detail_fragment_shows_blockers(tmp_path: Path) -> None:
    done_stem = ticket_mod.write_ticket(tmp_path, "Done dep", "").stem
    done_dep = ticket_mod.claim_next(tmp_path)
    assert done_dep is not None
    ticket_mod.finish(done_dep, tmp_path, "shipped", "ok", "run-1")
    dep_stem = ticket_mod.write_ticket(tmp_path, "Dep first", "").stem
    stem = ticket_mod.write_ticket(tmp_path, "Dependent", "", blocked_by=[dep_stem, done_stem]).stem

    body = TestClient(create_app(tmp_path)).get(f"/fragments/tickets/{stem}").text
    assert '<span class="pill pill-blocked">Dep first</span>' in body
    assert '<span class="pill">Done dep</span>' in body


def test_ticket_detail_fragment_non_queue_state(tmp_path: Path) -> None:
    ticket_mod.write_ticket(tmp_path, "Doomed", "")
    ticket = ticket_mod.claim_next(tmp_path)
    assert ticket is not None
    ticket_mod.finish(ticket, tmp_path, "failed", "boom", "run-1")
    stem = ticket_mod.list_tickets(tmp_path, "failed")[0].path.stem

    resp = TestClient(create_app(tmp_path)).get(f"/fragments/tickets/{stem}")
    assert resp.status_code == 200
    assert "failed" in resp.text


def test_ticket_detail_fragment_unknown_404(tmp_path: Path) -> None:
    assert TestClient(create_app(tmp_path)).get("/fragments/tickets/nope").status_code == 404


def test_ticket_detail_escapes_html(tmp_path: Path) -> None:
    stem = ticket_mod.write_ticket(tmp_path, "Sneaky", "<script>alert(1)</script>").stem

    body = TestClient(create_app(tmp_path)).get(f"/fragments/tickets/{stem}").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_board_cards_clickable(tmp_path: Path) -> None:
    stem = ticket_mod.write_ticket(tmp_path, "Open me", "").stem
    blocked_stem = ticket_mod.write_ticket(
        tmp_path, "Blocked", "", blocked_by=["no-such-stem"]
    ).stem

    body = TestClient(create_app(tmp_path)).get("/fragments/board").text
    assert f'data-ticket-detail="{stem}"' in body
    assert f'data-ticket-detail="{blocked_stem}"' in body
    assert f'draggable="true" data-ticket-id="{stem}"' in body
    assert f'data-ticket-id="{blocked_stem}"' not in body


def test_post_tickets_with_blockers(tmp_path: Path) -> None:
    stem1 = ticket_mod.write_ticket(tmp_path, "First blocker", "").stem
    stem2 = ticket_mod.write_ticket(tmp_path, "Second blocker", "").stem
    client = TestClient(create_app(tmp_path), follow_redirects=False)

    resp = client.post(
        "/tickets",
        data={
            "title": "Dep",
            "body": "",
            "workflow": "feature",
            "priority": "5",
            "blocked_by": [stem1, stem2],
        },
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/tickets?toast=ticket-created"
    dep = next(t for t in ticket_mod.list_tickets(tmp_path, "queue") if t.title == "Dep")
    assert dep.blocked_by == [stem1, stem2]


def test_new_ticket_modal_lists_blocker_options(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    assert 'name="blocked_by"' not in client.get("/tickets").text  # empty queue: no picker

    stem = ticket_mod.write_ticket(tmp_path, "Pickable", "").stem
    body = client.get("/tickets").text
    assert 'name="blocked_by"' in body
    assert f'<option value="{stem}">Pickable</option>' in body


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
    assert "No runs yet" in client.get("/runs").text


def test_dashboard_home_page(tmp_path: Path) -> None:
    _seed_run(tmp_path, "won", status="shipped")
    _seed_run(tmp_path, "lost", status="failed")
    _seed_run(tmp_path, "live", status="running")
    _seed_run(tmp_path, "gated", status="awaiting_plan_approval", pending_gate="plan")

    resp = TestClient(create_app(tmp_path)).get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "Needs attention" in body
    assert "Success rate" in body
    # the paused run is listed in the needs-attention card with a detail link
    assert '<a href="/runs/gated">gated</a>' in body
    assert "gate: plan" in body
    assert "50%" in body  # 1 shipped / 2 terminal
    assert "Cost by workflow" in body
    assert "feature" in body


def test_dashboard_empty_state(tmp_path: Path) -> None:
    resp = TestClient(create_app(tmp_path)).get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "Success rate" in body
    assert "—" in body  # None ratios render as an em dash
    assert "No runs yet" in body  # recent-runs card empty state
    assert "Nothing needs you" in body


def test_fragments_dashboard(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    ticket_mod.write_ticket(tmp_path, "Queued thing", "")

    body = TestClient(create_app(tmp_path)).get("/fragments/dashboard").text
    assert "r1" in body
    assert '<td>queue</td><td class="num mono">1</td>' in body
    assert "hx-" not in body


def test_dashboard_metrics_unit() -> None:
    def mk(run_id: str, status: str, cost: float = 0.0, fix: int = 0) -> RunState:
        state = RunState(run_id=run_id, workflow="feature", task="t", repo="/tmp")
        state.status = status  # type: ignore[assignment]
        state.add_cost(cost)
        state.fix_attempts = fix
        return state

    runs = [
        mk("a", "shipped", cost=2.0, fix=1),
        mk("b", "failed", cost=1.0, fix=3),
        mk("c", "running"),
        mk("d", "awaiting_plan_approval"),
    ]
    runs[3].pending_gate = "plan"

    metrics = views.dashboard_metrics(runs)
    counts = metrics["status_counts"]
    assert isinstance(counts, dict)
    assert counts["shipped"] == 1
    assert counts["failed"] == 1
    assert counts["cancelled"] == 0  # zero-filled: every status key present
    assert metrics["terminal"] == 2
    assert metrics["success_rate"] == 0.5
    attention = metrics["attention"]
    assert isinstance(attention, list)
    assert [r.run_id for r in attention] == ["d"]
    assert metrics["avg_cost"] == pytest.approx(0.75)
    assert metrics["avg_fix_attempts"] == pytest.approx(1.0)
    assert metrics["runs_24h"] == 4
    assert metrics["runs_7d"] == 4

    empty = views.dashboard_metrics([])
    assert empty["success_rate"] is None
    assert empty["avg_cost"] is None
    assert empty["avg_fix_attempts"] is None


def test_runs_nav_links(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    client = TestClient(create_app(tmp_path))

    home = client.get("/").text
    assert 'href="/runs"' in home
    assert '<a href="/" class="nav-link active">Dashboard</a>' in home

    runs_body = client.get("/runs").text
    assert "r1" in runs_body
    assert 'href="/runs" class="nav-link active"' in runs_body


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


def _two_repos(tmp_path: Path) -> tuple[Path, Path]:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    return alpha, beta


def test_multi_app_repo_isolation(tmp_path: Path) -> None:
    alpha, beta = _two_repos(tmp_path)
    _seed_run(alpha, "r1")
    ticket_mod.write_ticket(beta, "Beta ticket", "")
    client = TestClient(create_root_app([alpha, beta]))

    assert "r1" in client.get("/r/alpha/runs").text
    assert "r1" not in client.get("/r/beta/runs").text
    assert "Beta ticket" in client.get("/r/beta/tickets").text
    assert "Beta ticket" not in client.get("/r/alpha/tickets").text


def test_root_redirects_to_default_repo(tmp_path: Path) -> None:
    alpha, beta = _two_repos(tmp_path)
    client = TestClient(create_root_app([alpha, beta]))

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/r/alpha/"


def test_sidebar_switcher(tmp_path: Path) -> None:
    alpha, beta = _two_repos(tmp_path)
    body = TestClient(create_root_app([alpha, beta])).get("/r/alpha/").text
    assert 'href="/r/beta/"' in body
    assert '<a href="/r/alpha/" class="nav-link active">alpha</a>' in body

    solo = TestClient(create_root_app([alpha])).get("/r/alpha/").text
    assert '<a href="/r/alpha/" class="nav-link active">alpha</a>' in solo


def test_prefixed_urls_and_redirects(tmp_path: Path) -> None:
    alpha, beta = _two_repos(tmp_path)
    client = TestClient(create_root_app([alpha, beta]))

    body = client.get("/r/alpha/tickets").text
    assert 'action="/r/alpha/tickets"' in body
    assert 'hx-get="/r/alpha/fragments/board"' in body
    assert 'data-root="/r/alpha"' in body

    resp = client.post(
        "/r/alpha/tickets",
        data={"title": "Alpha task", "body": "", "workflow": "feature", "priority": "5"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/r/alpha/tickets?toast=ticket-created"
    assert [t.title for t in ticket_mod.list_tickets(alpha, "queue")] == ["Alpha task"]
    assert ticket_mod.list_tickets(beta, "queue") == []


def test_sse_frames_carry_prefix(tmp_path: Path) -> None:
    alpha, beta = _two_repos(tmp_path)
    _seed_run(alpha, "r1", status="awaiting_plan_approval", pending_gate="plan")

    resp = TestClient(create_root_app([alpha, beta])).get("/r/alpha/runs/r1/events")
    assert resp.status_code == 200
    # env-global root reaches the bare .render() calls on the SSE path
    assert "/r/alpha/runs/r1/approve" in resp.text


def test_slug_collision_mounts_both(tmp_path: Path) -> None:
    app_x = tmp_path / "x" / "app"
    app_y = tmp_path / "y" / "app"
    app_x.mkdir(parents=True)
    app_y.mkdir(parents=True)
    client = TestClient(create_root_app([app_x, app_y]))

    assert client.get("/r/app/").status_code == 200
    assert client.get("/r/app-2/").status_code == 200


def test_run_detail_under_mount(tmp_path: Path) -> None:
    alpha, beta = _two_repos(tmp_path)
    _seed_run(alpha, "r1", status="running")
    client = TestClient(create_root_app([alpha, beta]))

    resp = client.get("/r/alpha/runs/r1")
    assert resp.status_code == 200
    assert 'sse-connect="/r/alpha/runs/r1/events"' in resp.text
    assert client.get("/r/alpha/static/app.js").status_code == 200


def test_start_ticket_spawns_with_correct_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, beta = _two_repos(tmp_path)
    captured = _install_fake_popen(monkeypatch)
    stem = ticket_mod.write_ticket(beta, "Start me", "").stem
    client = TestClient(create_root_app([alpha, beta]))

    resp = client.post(f"/r/beta/tickets/{stem}/start", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/r/beta/tickets?toast=ticket-started"
    assert captured[-1][-2:] == ["--repo", str(beta)]


def test_python_m_adw_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "adw", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "adw" in result.stdout
