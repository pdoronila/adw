"""FastAPI app factory for the local adw dashboard.

All routes close over a single `repo` and are thin: reads go through `views`,
writes shell out through `runner` (detached CLI) or `tickets` directly.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from adw.config import load_config
from adw.queue import tickets as ticket_mod
from adw.state import run_state as rs
from adw.ui import runner, views

_HERE = Path(__file__).parent


class _NoCacheStaticFiles(StaticFiles):
    """Static files with `Cache-Control: no-cache` so browsers revalidate via ETag."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _asset_version(static_dir: Path) -> str:
    """Content hash of all static assets (not mtime — installs can normalize mtimes)."""
    blobs = sorted(p.name.encode() + p.read_bytes() for p in static_dir.iterdir() if p.is_file())
    return hashlib.md5(b"".join(blobs)).hexdigest()[:8]


def app_factory() -> FastAPI:
    """Entry point for uvicorn's reloader (`adw ui --reload`).

    The reloader needs an import string and re-imports in a fresh process, so
    the repo path travels via the ADW_UI_REPO env var set by the CLI.
    """
    return create_app(Path(os.environ.get("ADW_UI_REPO", ".")).resolve())


def create_app(repo: Path) -> FastAPI:
    app = FastAPI(title="adw")
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    templates.env.globals["pill_class"] = views.pill_class
    templates.env.globals["humanize_ts"] = views.humanize_ts
    templates.env.globals["clock_ts"] = views.clock_ts
    templates.env.globals["asset_v"] = _asset_version(_HERE / "static")
    app.mount("/static", _NoCacheStaticFiles(directory=str(_HERE / "static")), name="static")

    def _board_context() -> dict[str, object]:
        """Everything the board template needs, plus queue-derived page context."""
        board = {state: ticket_mod.list_tickets(repo, state) for state in ticket_mod.STATES}
        done = ticket_mod.done_stems(repo)
        titles = {t.id: t.title for tickets in board.values() for t in tickets}
        blockers: dict[str, list[str]] = {}
        for ticket in board["queue"]:
            pending = ticket_mod.pending_blockers(ticket, done)
            if pending:
                blockers[ticket.id] = [titles.get(stem, stem) for stem in pending]
        return {
            "board": board,
            "ticket_states": ticket_mod.STATES,
            "blockers": blockers,
            "queue_count": len(board["queue"]),
            "blocker_options": [
                (t.id, t.title) for state in ("queue", "in_progress") for t in board[state]
            ],
            # Separate key, not a board column: archived stays off the 4-column grid.
            "archived": ticket_mod.list_tickets(repo, ticket_mod.ARCHIVED),
        }

    def _page_context(
        active_page: str, toast: str, runs: list[rs.RunState] | None = None
    ) -> dict[str, object]:
        if runs is None:
            runs = views.list_runs(repo)
        return {
            **_board_context(),
            "run_count": len(runs),
            "total_spend": sum(r.total_cost_usd for r in runs),
            "workflows": views.workflow_options(),
            "ticket_workflows": views.ticket_workflow_options(),
            "toast_message": views.toast_message(toast),
            "active_page": active_page,
        }

    def _gate_rounds(state: rs.RunState) -> list[dict[str, object]]:
        rounds: list[dict[str, object]] = []
        for round_ in state.gate_results:
            raw = round_.get("results")
            results = [
                {
                    "name": r.get("name"),
                    "ok": r.get("ok"),
                    "exit_code": r.get("exit_code"),
                }
                for r in (raw if isinstance(raw, list) else [])
            ]
            rounds.append({"attempt": round_.get("attempt"), "results": results})
        return rounds

    def _dashboard_context(runs: list[rs.RunState]) -> dict[str, object]:
        """Dashboard metrics plus page-level extras, from one already-listed `runs`."""
        try:
            budget = load_config(repo).limits.max_cost_usd
        except Exception:
            budget = None  # a broken adw.yaml must never 500 the dashboard
        return {
            **views.dashboard_metrics(runs),
            "recent_runs": runs[:8],
            "run_count": len(runs),
            "budget": budget,
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard_page(request: Request, toast: str = "") -> HTMLResponse:
        runs = views.list_runs(repo)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {**_page_context("dashboard", toast, runs=runs), **_dashboard_context(runs)},
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(
        request: Request, q: str = "", status: str = "", toast: str = ""
    ) -> HTMLResponse:
        runs = views.list_runs(repo)
        return templates.TemplateResponse(
            request,
            "runs.html",
            {
                **_page_context("runs", toast),
                "runs": views.filter_runs(runs, q, status),
                "run_count": len(runs),
                "q": q,
                "status": status,
            },
        )

    @app.get("/tickets", response_class=HTMLResponse)
    def tickets_page(request: Request, toast: str = "") -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "tickets.html",
            _page_context("tickets", toast),
        )

    @app.get("/fragments/runs", response_class=HTMLResponse)
    def fragment_runs(request: Request, q: str = "", status: str = "") -> HTMLResponse:
        runs = views.list_runs(repo)
        return templates.TemplateResponse(
            request,
            "_runs_table.html",
            {
                "runs": views.filter_runs(runs, q, status),
                "run_count": len(runs),
            },
        )

    @app.get("/fragments/dashboard", response_class=HTMLResponse)
    def fragment_dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_dashboard.html",
            {**_dashboard_context(views.list_runs(repo)), **_board_context()},
        )

    @app.get("/fragments/board", response_class=HTMLResponse)
    def fragment_board(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_board.html",
            _board_context(),
        )

    @app.get("/fragments/tickets/{ticket_id}", response_class=HTMLResponse)
    def fragment_ticket_detail(request: Request, ticket_id: str) -> HTMLResponse:
        try:
            ticket = ticket_mod.find_ticket(repo, ticket_id, ticket_mod.FIND_STATES)
        except ticket_mod.TicketError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request, "_ticket_detail.html", views.ticket_detail_context(repo, ticket)
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str, toast: str = "") -> HTMLResponse:
        state = views.get_state(repo, run_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
        run_dir = rs.runs_root(repo) / run_id
        return templates.TemplateResponse(
            request,
            "run_detail.html",
            {
                **_page_context("run_detail", toast),
                "state": state,
                "plan_html": views.render_markdown_file(run_dir, "plan.md"),
                "review_html": views.render_markdown_file(run_dir, "review.md"),
                "gate_rounds": _gate_rounds(state),
                "gate_logs": views.gate_logs(run_dir),
                "changed_files": views.changed_files(state),
                "transcripts": views.agent_transcripts(run_dir),
                "live": state.status not in views.TERMINAL and state.status not in views.PAUSED,
            },
        )

    @app.get("/runs/{run_id}/events")
    def run_events(run_id: str) -> StreamingResponse:
        timeline = templates.get_template("_timeline.html")
        run_head = templates.get_template("_run_head.html")
        plan = templates.get_template("_plan.html")
        run_dir = rs.runs_root(repo) / run_id

        def render(state: rs.RunState) -> list[tuple[str, str]]:
            return [
                ("timeline", timeline.render(state=state, pill_class=views.pill_class)),
                ("runhead", run_head.render(state=state)),
                ("plan", plan.render(plan_html=views.render_markdown_file(run_dir, "plan.md"))),
            ]

        gen = views.timeline_events(repo, run_id, render)
        return StreamingResponse(gen, media_type="text/event-stream")

    @app.post("/runs")
    def create_run(
        workflow: str = Form(...),
        task: str = Form(...),
        model: str = Form(""),
        backend: str = Form(""),
        isolation: str = Form(""),
    ) -> RedirectResponse:
        run_id = rs.new_run_id(task)
        runner.start_run(repo, run_id, workflow, task, model, backend, isolation)
        return RedirectResponse(f"/runs/{run_id}?toast=run-started", status_code=303)

    @app.post("/runs/{run_id}/approve")
    def approve_run(run_id: str) -> RedirectResponse:
        runner.resume_run(repo, run_id, "approve")
        return RedirectResponse(f"/runs/{run_id}?toast=approved", status_code=303)

    @app.post("/runs/{run_id}/reject")
    def reject_run(run_id: str) -> RedirectResponse:
        runner.resume_run(repo, run_id, "reject")
        return RedirectResponse(f"/runs/{run_id}?toast=rejected", status_code=303)

    @app.post("/runs/{run_id}/retry")
    def retry_run(run_id: str) -> RedirectResponse:
        runner.retry_run(repo, run_id)
        return RedirectResponse(f"/runs/{run_id}?toast=retry-started", status_code=303)

    @app.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> RedirectResponse:
        runner.cancel_run(repo, run_id)
        return RedirectResponse(f"/runs/{run_id}?toast=cancel-requested", status_code=303)

    @app.post("/tickets")
    def create_ticket(
        title: str = Form(...),
        body: str = Form(""),
        workflow: str = Form("feature"),
        priority: int = Form(ticket_mod.DEFAULT_PRIORITY),
        blocked_by: Annotated[list[str] | None, Form()] = None,
    ) -> RedirectResponse:
        clean = [b.strip() for b in blocked_by or [] if b.strip()]
        ticket_mod.write_ticket(
            repo, title, body, workflow=workflow, priority=int(priority), blocked_by=clean or None
        )
        return RedirectResponse("/tickets?toast=ticket-created", status_code=303)

    @app.post("/tickets/{ticket_id}/delete")
    def delete_ticket(ticket_id: str) -> RedirectResponse:
        try:
            ticket = ticket_mod.find_ticket(repo, ticket_id, ticket_mod.FIND_STATES)
        except ticket_mod.TicketError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        ticket_mod.remove(ticket)
        return RedirectResponse("/tickets?toast=ticket-deleted", status_code=303)

    @app.post("/tickets/{ticket_id}/requeue")
    def requeue_ticket(ticket_id: str) -> RedirectResponse:
        try:
            ticket = ticket_mod.find_ticket(
                repo, ticket_id, ("failed", "done", ticket_mod.ARCHIVED)
            )
        except ticket_mod.TicketError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        ticket_mod.requeue(repo, ticket)
        return RedirectResponse("/tickets?toast=ticket-requeued", status_code=303)

    @app.post("/tickets/{ticket_id}/archive")
    def archive_ticket(ticket_id: str) -> RedirectResponse:
        try:
            ticket = ticket_mod.find_ticket(repo, ticket_id, ("done",))
        except ticket_mod.TicketError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        ticket_mod.archive(repo, ticket)
        return RedirectResponse("/tickets?toast=ticket-archived", status_code=303)

    @app.post("/queue/process")
    def queue_process() -> RedirectResponse:
        runner.process_queue(repo)
        return RedirectResponse("/tickets?toast=queue-processing", status_code=303)

    @app.post("/tickets/{ticket_id}/start")
    def start_ticket(ticket_id: str) -> RedirectResponse:
        # Pre-check for good UX only — the spawned CLI's claim_ticket is the
        # authoritative, atomic check (a lost race just logs to ui.log).
        try:
            ticket = ticket_mod.find_ticket(repo, ticket_id, ("queue",))
        except ticket_mod.TicketError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if ticket_mod.pending_blockers(ticket, ticket_mod.done_stems(repo)):
            return RedirectResponse("/tickets?toast=ticket-blocked", status_code=303)
        runner.start_ticket(repo, ticket.id)
        return RedirectResponse("/tickets?toast=ticket-started", status_code=303)

    return app
