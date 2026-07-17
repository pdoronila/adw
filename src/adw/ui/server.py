"""FastAPI app factory for the local adw dashboard.

All routes close over a single `repo` and are thin: reads go through `views`,
writes shell out through `runner` (detached CLI) or `tickets` directly.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from adw.queue import tickets as ticket_mod
from adw.state import run_state as rs
from adw.ui import runner, views

_HERE = Path(__file__).parent


def create_app(repo: Path) -> FastAPI:
    app = FastAPI(title="adw")
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    templates.env.globals["pill_class"] = views.pill_class
    templates.env.globals["humanize_ts"] = views.humanize_ts
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    def _board() -> dict[str, list[ticket_mod.Ticket]]:
        return {state: ticket_mod.list_tickets(repo, state) for state in ticket_mod.STATES}

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

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request, q: str = "", status: str = "", toast: str = ""
    ) -> HTMLResponse:
        runs = views.list_runs(repo)
        board = _board()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "runs": views.filter_runs(runs, q, status),
                "run_count": len(runs),
                "queue_count": len(board["queue"]),
                "q": q,
                "status": status,
                "board": board,
                "ticket_states": ticket_mod.STATES,
                "workflows": views.workflow_options(),
                "toast_message": views.toast_message(toast),
                "active_page": "dashboard",
            },
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

    @app.get("/fragments/board", response_class=HTMLResponse)
    def fragment_board(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_board.html",
            {"board": _board(), "ticket_states": ticket_mod.STATES},
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
                "state": state,
                "toast_message": views.toast_message(toast),
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

        def render(state: rs.RunState) -> list[tuple[str, str]]:
            return [
                ("timeline", timeline.render(state=state, pill_class=views.pill_class)),
                ("runhead", run_head.render(state=state)),
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
    ) -> RedirectResponse:
        ticket_mod.write_ticket(repo, title, body, workflow=workflow, priority=int(priority))
        return RedirectResponse("/?toast=ticket-created", status_code=303)

    @app.post("/tickets/{ticket_id}/delete")
    def delete_ticket(ticket_id: str) -> RedirectResponse:
        try:
            ticket = ticket_mod.find_ticket(repo, ticket_id)
        except ticket_mod.TicketError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        ticket_mod.remove(ticket)
        return RedirectResponse("/?toast=ticket-deleted", status_code=303)

    @app.post("/tickets/{ticket_id}/requeue")
    def requeue_ticket(ticket_id: str) -> RedirectResponse:
        try:
            ticket = ticket_mod.find_ticket(repo, ticket_id, ("failed", "done"))
        except ticket_mod.TicketError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        ticket_mod.requeue(repo, ticket)
        return RedirectResponse("/?toast=ticket-requeued", status_code=303)

    @app.post("/queue/process")
    def queue_process() -> RedirectResponse:
        runner.process_queue(repo)
        return RedirectResponse("/?toast=queue-processing", status_code=303)

    return app
