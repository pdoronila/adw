"""adw — AI Developer Workflows CLI."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import typer

from adw import __version__, routing
from adw.adapters import ADAPTERS, get_adapter
from adw.adapters.base import AgentInvocation
from adw.config import AdwConfig, load_config
from adw.exec_env import make_env
from adw.nodes import git_ops
from adw.nodes.agent_node import AgentRunner
from adw.queue import tickets as ticket_mod
from adw.state import run_state as rs
from adw.workflows import WORKFLOWS, RunOutcome, WorkflowContext, get_workflow

app = typer.Typer(
    help="AI Developer Workflows: code + agents + you, composed.", no_args_is_help=True
)
queue_app = typer.Typer(help="Process the file-based ticket queue.", no_args_is_help=True)
ticket_app = typer.Typer(help="Create and inspect tickets.", no_args_is_help=True)
sandbox_app = typer.Typer(help="Manage the Apple container sandbox image.", no_args_is_help=True)
app.add_typer(queue_app, name="queue")
app.add_typer(ticket_app, name="ticket")
app.add_typer(sandbox_app, name="sandbox")

REPO_OPT = typer.Option(Path("."), "--repo", help="Target repository", resolve_path=True)
BLOCKED_BY_OPT = typer.Option(
    None, "--blocked-by", help="Ticket stem this ticket waits on (repeatable)"
)


def _load(repo: Path) -> AdwConfig:
    try:
        return load_config(repo)
    except Exception as exc:
        typer.secho(f"config error: {exc}", fg="red")
        raise typer.Exit(2) from exc


_STATUS_COLOR = {
    "shipped": "green",
    "rejected": "yellow",
    "failed": "red",
    "paused": "cyan",
    "cancelled": "yellow",
}


def _report(state: rs.RunState, outcome: RunOutcome, run_dir: Path) -> None:
    color = _STATUS_COLOR[outcome.status]
    typer.secho(f"■ {outcome.status}: {outcome.reason}", fg=color, bold=True)
    for hint in outcome.hints:
        typer.echo(f"  ↳ {hint}")
    if state.worktree and outcome.status not in ("shipped", "rejected"):
        typer.echo(f"  worktree kept for salvage: {state.worktree}")
    if state.total_cost_usd:
        typer.echo(f"  agent cost: ${state.total_cost_usd:.2f}")
    typer.echo(f"  artifacts: {run_dir}")


def _cleanup_isolation(state: rs.RunState) -> None:
    """Remove a per-run worktree once its work is safely on the branch (shipped)."""
    if state.worktree and state.status == "shipped":
        from adw.nodes import git_ops

        git_ops.remove_worktree(Path(state.repo), Path(state.worktree))


def _execute(
    workflow_name: str,
    task: str,
    repo: Path,
    config: AdwConfig,
    auto_approve_plan: bool,
    assume_yes: bool,
    async_mode: bool = False,
    run_suffix: str = "",
    run_id: str | None = None,
    source_ticket_run: str | None = None,
) -> rs.RunState:
    workflow = get_workflow(workflow_name)
    if run_id is None:
        run_id = rs.new_run_id(task) + run_suffix
    run_dir = rs.create_run_dir(repo, run_id)
    state = rs.RunState(
        run_id=run_id,
        workflow=workflow_name,
        task=task,
        repo=str(repo),
        source_ticket_run=source_ticket_run,
    )
    state.pid = os.getpid()
    state.pgid = os.getpgid(0)
    rs.save_state(state, run_dir)
    env = make_env(config)
    ctx = WorkflowContext(
        repo_dir=repo,
        run_dir=run_dir,
        config=config,
        state=state,
        task=task,
        agents=AgentRunner(config, run_dir, workflow=workflow_name, env=env),
        auto_approve_plan=auto_approve_plan,
        assume_yes=assume_yes,
        mode="async" if async_mode else "interactive",
        env=env,
    )
    typer.secho(f"▶ run {run_id} [{workflow_name}] in {repo}", bold=True)
    outcome = workflow.run(ctx)
    _report(state, outcome, run_dir)
    _cleanup_isolation(state)
    return state


@app.command()
def run(
    workflow: str = typer.Argument(help="Workflow name (see `adw workflows`)"),
    task: str = typer.Argument(help="What to do, in plain language"),
    repo: Path = REPO_OPT,
    auto_approve_plan: bool = typer.Option(False, help="Skip engineer gate 1 (plan approval)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip BOTH engineer gates (unattended)"),
    async_mode: bool = typer.Option(
        False, "--async", help="Pause at engineer gates instead of blocking; resume later"
    ),
    race: int = typer.Option(
        1, help="Run N isolated candidates concurrently; first to pass gates wins"
    ),
    max_iterations: int | None = typer.Option(None, help="Override workflow.max_fix_iterations"),
    model: str | None = typer.Option(None, help="Override the model for all agent roles"),
    backend: str | None = typer.Option(None, help="Override the backend for all agent roles"),
    isolation: str | None = typer.Option(
        None, help="Override isolation.type: local|worktree|container"
    ),
    dry_run: bool = typer.Option(False, help="Print the resolved plan of execution and exit"),
    run_id: str | None = typer.Option(None, "--run-id", hidden=True, help="Preassigned run id"),
) -> None:
    """Run one AI developer workflow end to end."""
    config = _load(repo)
    if max_iterations is not None:
        config.workflow.max_fix_iterations = max_iterations
    if model is not None:
        config.agents.default.model = model
        for role_agent in config.agents.roles.values():
            role_agent.model = model  # pinned roles get the override, not the backend default
    if backend is not None:
        if backend not in ADAPTERS:
            valid = ", ".join(sorted(ADAPTERS))
            typer.secho(f"unknown backend {backend!r}; valid backends: {valid}", fg="red")
            raise typer.Exit(2)
        config.agents.default.backend = backend
        for role_agent in config.agents.roles.values():
            role_agent.backend = backend  # override every role, including pinned ones
    if isolation is not None:
        valid_isolation = ("local", "worktree", "container")
        if isolation not in valid_isolation:
            typer.secho(
                f"unknown isolation {isolation!r}; valid: {', '.join(valid_isolation)}", fg="red"
            )
            raise typer.Exit(2)
        config.isolation.type = isolation  # type: ignore[assignment]
    if dry_run:
        _print_dry_run(workflow, task, repo, config, model, backend, isolation)
        return
    if race > 1:
        winner = _race(workflow, task, repo, config, race)
        raise typer.Exit(0 if winner is not None else 1)
    state = _execute(
        workflow, task, repo, config, auto_approve_plan, yes, async_mode, run_id=run_id
    )
    raise typer.Exit(0 if state.status in ("shipped", "paused") else 1)


def _race(workflow: str, task: str, repo: Path, config: AdwConfig, n: int) -> rs.RunState | None:
    """Run N isolated candidates concurrently; the first to ship wins.

    Requires worktree/container isolation so candidates don't collide. Candidates
    run unattended; the first one whose gates pass is the winner. (Losers run to
    completion — mid-flight cancellation of a running agent is a future refinement.)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if config.isolation.type == "local":
        typer.secho("--race needs isolation.type: worktree (or container)", fg="red")
        raise typer.Exit(2)
    typer.secho(f"racing {n} candidates for: {task}", bold=True)

    def candidate(i: int) -> rs.RunState:
        return _execute(workflow, task, repo, config, True, True, run_suffix=f"-r{i}")

    winner: rs.RunState | None = None
    others: list[rs.RunState] = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(candidate, i) for i in range(n)]
        for future in as_completed(futures):
            state = future.result()
            if state.status == "shipped" and winner is None:
                winner = state
            else:
                others.append(state)

    if winner is None:
        typer.secho("no candidate shipped", fg="red")
        return None
    typer.secho(f"\n🏁 winner: {winner.work_branch} (run {winner.run_id})", fg="green", bold=True)
    # Losers' branches are extra; drop them so only the winner remains.
    for state in others:
        if state.work_branch and state.work_branch != winner.work_branch:
            from adw.nodes import git_ops

            if state.worktree:
                git_ops.remove_worktree(repo, Path(state.worktree))
            git_ops.delete_branch(repo, state.work_branch)
    return winner


def _print_dry_run(
    workflow: str,
    task: str,
    repo: Path,
    config: AdwConfig,
    model: str | None = None,
    backend: str | None = None,
    isolation: str | None = None,
) -> None:
    get_workflow(workflow)  # validate name
    typer.secho(f"workflow: {workflow}", bold=True)
    typer.echo(f"task: {task}")
    typer.echo(f"repo: {repo}")
    overrides = [
        f"{k}={v}"
        for k, v in (("model", model), ("backend", backend), ("isolation", isolation))
        if v is not None
    ]
    if overrides:
        typer.echo(f"overrides: {', '.join(overrides)}")
    typer.echo("agent roles:")
    for role in ("scout", "plan", "build", "review"):
        ra = config.resolve_role(role)
        typer.echo(f"  {role:<8} -> {ra.backend} (model: {ra.model or 'backend default'})")
    typer.echo(f"gates (order): {', '.join(config.gate_order()) or '(none configured!)'}")
    for name in config.gate_order():
        gate = config.gates[name]
        typer.echo(f"  {name:<10} $ {gate.command}  (timeout {gate.timeout}s)")
    typer.echo(f"max fix iterations: {config.workflow.max_fix_iterations}")
    typer.echo(f"isolation: {config.isolation.type}")
    typer.echo(f"ship: branch_prefix={config.ship.branch_prefix} create_pr={config.ship.create_pr}")


@app.command()
def route(
    task: str = typer.Argument(help="Ticket / request in plain language"),
    repo: Path = REPO_OPT,
    run: bool = typer.Option(False, help="Run the chosen workflow instead of just printing it"),
    auto_approve_plan: bool = typer.Option(False, help="Skip engineer gate 1 when --run"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip both engineer gates when --run"),
    json_output: bool = typer.Option(False, "--json", help="Print the routing result as JSON."),
) -> None:
    """Classify a request into the right workflow (the factory router)."""
    config = _load(repo)
    result = routing.route(task, config, repo)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "workflow": result.workflow,
                    "task": result.task,
                    "rationale": result.rationale,
                    "method": result.method,
                },
                indent=2,
            )
        )
        return
    typer.secho(f"→ {result.workflow}  ({result.method})", fg="cyan", bold=True)
    typer.echo(f"  rationale: {result.rationale}")
    if result.task != task:
        typer.echo(f"  refined task: {result.task}")
    if not run:
        typer.echo(f"\nrun it: adw run {result.workflow} {result.task!r} --repo {repo}")
        return
    state = _execute(result.workflow, result.task, repo, config, auto_approve_plan, yes)
    raise typer.Exit(0 if state.status == "shipped" else 1)


@app.command()
def workflows(
    json_output: bool = typer.Option(False, "--json", help="Print the workflows list as JSON."),
) -> None:
    """List registered workflows."""
    if json_output:
        typer.echo(
            json.dumps(
                [
                    {"name": name, "description": wf.description}
                    for name, wf in sorted(WORKFLOWS.items())
                ],
                indent=2,
            )
        )
        return
    for name, wf in sorted(WORKFLOWS.items()):
        typer.echo(f"{name:<10} {wf.description}")


@app.command()
def status(
    run_id: str = typer.Argument(None),
    repo: Path = REPO_OPT,
    json_output: bool = typer.Option(False, "--json", help="Print the runs list as JSON."),
    diff: bool = typer.Option(
        False, "--diff", help="Print git diff base_branch..work_branch for the run."
    ),
    costs: bool = typer.Option(
        False, "--costs", help="Print total and per-workflow cost rollups across all runs."
    ),
) -> None:
    """Show recent runs, or full detail for one run."""
    if diff and not run_id:
        typer.secho("--diff requires a run id", fg="red", err=True)
        raise typer.Exit(1)
    if costs and run_id:
        typer.secho("--costs cannot be combined with a run id", fg="red", err=True)
        raise typer.Exit(1)
    if costs:
        rollup = rs.cost_rollup(rs.list_runs(repo))
        if json_output:
            typer.echo(rollup.model_dump_json(indent=2))
            return
        if rollup.runs == 0:
            typer.echo("no runs yet")
            return
        for name, wc in sorted(rollup.workflows.items()):
            typer.echo(f"{name:<10} {wc.runs:>4} runs  ${wc.total_cost_usd:>8.2f}")
        typer.echo(f"{'total':<10} {rollup.runs:>4} runs  ${rollup.total_cost_usd:>8.2f}")
        return
    if run_id:
        run_dir = rs.runs_root(repo) / run_id
        if not (run_dir / "state.json").is_file():
            typer.secho(f"no run {run_id!r} under {rs.runs_root(repo)}", fg="red")
            raise typer.Exit(1)
        if diff:
            state = rs.load_state(run_dir)
            target = Path(state.repo)
            if not state.base_branch or not state.work_branch:
                typer.secho(f"run {run_id!r} has no work branch yet (no diff)", fg="red", err=True)
                raise typer.Exit(1)
            if not target.is_dir() or not git_ops.is_git_repo(target):
                typer.secho(f"repo {state.repo!r} not found or not a git repo", fg="red", err=True)
                raise typer.Exit(1)
            if not git_ops.branch_exists(target, state.work_branch) or not git_ops.branch_exists(
                target, state.base_branch
            ):
                typer.secho(
                    f"branch {state.work_branch!r} (or base {state.base_branch!r}) no longer exists"
                    " — it may have been deleted after reject",
                    fg="red",
                    err=True,
                )
                raise typer.Exit(1)
            typer.echo(git_ops.branch_diff(target, state.base_branch, state.work_branch), nl=False)
            return
        typer.echo(json.dumps(json.loads((run_dir / "state.json").read_text()), indent=2))
        return
    states = rs.list_runs(repo)
    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "run_id": state.run_id,
                        "workflow": state.workflow,
                        "status": state.status,
                        "total_cost_usd": state.total_cost_usd,
                        "outcome_detail": state.outcome_detail,
                    }
                    for state in states[-20:]
                ],
                indent=2,
            )
        )
        return
    if not states:
        typer.echo("no runs yet")
        return
    for state in states[-20:]:
        typer.echo(
            f"{state.run_id:<42} {state.workflow:<8} {state.status:<24} "
            f"${state.total_cost_usd:>6.2f}  {state.outcome_detail[:60]}"
        )


@app.command()
def logs(
    run_id: str = typer.Argument(help="Run id to inspect"),
    repo: Path = REPO_OPT,
    tail: int = typer.Option(500, "--tail", help="Max characters of each agent output to show"),
) -> None:
    """Pretty-print one run's step timeline, agent transcripts, and gate results."""
    run_dir = rs.runs_root(repo) / run_id
    if not (run_dir / "state.json").is_file():
        typer.secho(f"no run {run_id!r} under {rs.runs_root(repo)}", fg="red")
        raise typer.Exit(1)
    state = rs.load_state(run_dir)

    typer.secho(
        f"■ {run_id}  [{state.workflow}]  {state.status}",
        fg=_STATUS_COLOR.get(state.status, "white"),
        bold=True,
    )
    typer.echo(f"  task: {state.task}")

    typer.secho("\nsteps:", bold=True)
    for step in state.steps:
        typer.echo(f"  {step.status:<8} {step.name:<24} {step.detail}")

    typer.secho("\nagents:", bold=True)
    agent_dir = run_dir / "agent"
    for path in sorted(agent_dir.glob("*.json")) if agent_dir.is_dir() else []:
        artifact = json.loads(path.read_text())
        cost = artifact.get("cost_usd") or 0.0
        role, model = artifact.get("role"), artifact.get("model")
        typer.echo(f"  {path.stem}  role={role} model={model} cost=${cost:.2f}")
        output = (artifact.get("output") or "")[:tail]
        for line in output.splitlines() or [""]:
            typer.echo(f"    {line}")

    typer.secho("\ngates:", bold=True)
    for round_ in state.gate_results:
        attempt = round_.get("attempt")
        results = round_.get("results")
        for result in results if isinstance(results, list) else []:
            mark = "✓" if result.get("ok") else "✗"
            name, exit_code = result.get("name"), result.get("exit_code")
            typer.echo(f"  attempt {attempt}  {mark} {name} (exit {exit_code})")

    typer.echo(f"\ntotal cost: ${state.total_cost_usd:.2f}")
    typer.echo(f"artifacts: {run_dir}")


@app.command()
def resume(
    run_id: str = typer.Argument(help="Run id from a paused (--async) run"),
    repo: Path = REPO_OPT,
    approve: bool = typer.Option(False, "--approve", help="Approve the pending gate"),
    reject: bool = typer.Option(False, "--reject", help="Reject the pending gate"),
    edit: bool = typer.Option(False, "--edit", help="Edit the pending artifact, then approve"),
) -> None:
    """Resume a paused run by answering its pending engineer gate."""
    run_dir = rs.runs_root(repo) / run_id
    if not (run_dir / "state.json").is_file():
        typer.secho(f"no run {run_id!r} under {rs.runs_root(repo)}", fg="red")
        raise typer.Exit(1)
    state = rs.load_state(run_dir)
    if state.pending_gate is None or state.status not in (
        "awaiting_plan_approval",
        "awaiting_final_review",
        "paused",
    ):
        typer.secho(f"run {run_id} is not paused (status={state.status})", fg="red")
        raise typer.Exit(1)
    if sum([approve, reject, edit]) != 1:
        typer.secho("pass exactly one of --approve / --reject / --edit", fg="red")
        raise typer.Exit(2)
    if edit and state.pending_gate == "budget":
        typer.secho("the budget gate has no artifact to edit; use --approve or --reject", fg="red")
        raise typer.Exit(2)
    if edit:
        artifact = run_dir / ("plan.md" if state.pending_gate == "plan" else "review.md")
        subprocess.run([os.environ.get("EDITOR", "vi"), str(artifact)])
    decision = "reject" if reject else "approve"

    target_repo = Path(state.repo)
    config = _load(target_repo)
    env = make_env(config)
    ctx = WorkflowContext(
        repo_dir=target_repo,
        run_dir=run_dir,
        config=config,
        state=state,
        task=state.task,
        agents=AgentRunner(config, run_dir, workflow=state.workflow, env=env),
        mode="async",
        decision=decision,  # type: ignore[arg-type]
        env=env,
    )
    typer.secho(
        f"▶ resume {run_id} [{state.workflow}] {decision} {state.pending_gate} gate", bold=True
    )
    outcome = get_workflow(state.workflow).run(ctx)
    _report(state, outcome, run_dir)
    _cleanup_isolation(state)
    raise typer.Exit(0 if state.status in ("shipped", "paused") else 1)


@app.command()
def retry(
    run_id: str = typer.Argument(help="Run id of a failed run"),
    repo: Path = REPO_OPT,
) -> None:
    """Retry a failed run from where it failed."""
    run_dir = rs.runs_root(repo) / run_id
    if not (run_dir / "state.json").is_file():
        typer.secho(f"no run {run_id!r} under {rs.runs_root(repo)}", fg="red")
        raise typer.Exit(1)
    state = rs.load_state(run_dir)
    if state.status != "failed":
        typer.secho(f"run {run_id} is not failed (status={state.status})", fg="red")
        raise typer.Exit(1)

    state.status = "running"
    state.outcome_detail = ""
    state.pid = os.getpid()
    state.pgid = os.getpgid(0)
    for record in state.steps:
        if record.status == "failed":
            record.status = "pending"
    rs.save_state(state, run_dir)

    target_repo = Path(state.repo)
    config = _load(target_repo)
    env = make_env(config)
    ctx = WorkflowContext(
        repo_dir=target_repo,
        run_dir=run_dir,
        config=config,
        state=state,
        task=state.task,
        agents=AgentRunner(config, run_dir, workflow=state.workflow, env=env),
        mode="async",
        env=env,
    )
    typer.secho(f"▶ retry {run_id} [{state.workflow}]", bold=True)
    outcome = get_workflow(state.workflow).run(ctx)
    _report(state, outcome, run_dir)
    _cleanup_isolation(state)
    raise typer.Exit(0 if state.status in ("shipped", "paused") else 1)


@app.command()
def land(
    run_id: str = typer.Argument(help="Run id of a shipped run"),
    repo: Path = REPO_OPT,
    push: bool = typer.Option(True, "--push/--no-push", help="Push the base branch to origin"),
    keep_branch: bool = typer.Option(
        False, "--keep-branch", help="Keep the work branch after landing"
    ),
) -> None:
    """Land a shipped run: rebase its work branch onto the base branch and ff-merge it."""
    run_dir = rs.runs_root(repo) / run_id
    if not (run_dir / "state.json").is_file():
        typer.secho(f"no run {run_id!r} under {rs.runs_root(repo)}", fg="red")
        raise typer.Exit(1)
    state = rs.load_state(run_dir)
    if state.status != "shipped":
        typer.secho(f"run {run_id} is not shipped (status={state.status})", fg="red")
        raise typer.Exit(1)

    from adw.workflows import steps

    try:
        landed, detail = steps.land(state, push=push, keep_branch=keep_branch)
    except git_ops.GitError as exc:
        typer.secho(f"land failed: {exc}", fg="red")
        raise typer.Exit(1) from exc
    state.outcome_detail = f"{state.outcome_detail}; {detail}" if state.outcome_detail else detail
    rs.save_state(state, run_dir)
    if not landed:
        typer.secho(f"■ {detail}", fg="yellow", bold=True)
        raise typer.Exit(1)
    typer.secho(f"■ {detail}", fg="green", bold=True)


@app.command()
def cancel(
    run_id: str = typer.Argument(help="Run id of a running run"),
    repo: Path = REPO_OPT,
) -> None:
    """Cancel a running run: SIGTERM its process group, keep branch/worktree for salvage."""
    run_dir = rs.runs_root(repo) / run_id
    if not (run_dir / "state.json").is_file():
        typer.secho(f"no run {run_id!r} under {rs.runs_root(repo)}", fg="red")
        raise typer.Exit(1)
    state = rs.load_state(run_dir)
    if state.status != "running":
        typer.secho(f"run {run_id} is not running (status={state.status})", fg="red")
        raise typer.Exit(1)

    if state.pgid is None:
        typer.secho(
            f"run {run_id} has no recorded pgid; marking cancelled without signal", fg="yellow"
        )
    else:
        try:
            os.killpg(state.pgid, signal.SIGTERM)
        except ProcessLookupError:
            typer.secho("process group already gone", fg="yellow")

    state.status = "cancelled"
    state.outcome_detail = "cancelled by user"
    rs.save_state(state, run_dir)
    typer.secho(f"■ cancelled: {run_id}", fg="yellow", bold=True)
    if state.worktree:
        typer.echo(f"  worktree kept for salvage: {state.worktree}")


@app.command()
def ui(
    repo: Path = REPO_OPT,
    port: int = typer.Option(8770, "--port"),
    host: str = typer.Option("127.0.0.1", "--host"),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open the browser"),
) -> None:
    """Serve the local web dashboard."""
    try:
        import uvicorn

        from adw.ui.server import create_app
    except ImportError as exc:
        typer.secho("adw ui needs the UI extra — install with: pip install 'adw[ui]'", fg="red")
        raise typer.Exit(2) from exc
    app_ = create_app(repo)
    if not no_open:
        import webbrowser

        webbrowser.open(f"http://{host}:{port}/")
    uvicorn.run(app_, host=host, port=port)


@app.command()
def doctor(repo: Path = REPO_OPT) -> None:
    """Check backends, config, and gates for this repo."""
    failures = 0
    typer.secho("backends:", bold=True)
    config = _load(repo)
    for name in sorted(ADAPTERS):
        binary = config.backends.for_backend(name).binary
        path = shutil.which(binary)
        if path:
            version = (
                subprocess.run([binary, "--version"], capture_output=True, text=True)
                .stdout.strip()
                .splitlines()
            )
            typer.secho(f"  ✓ {name:<12} {version[0] if version else path}", fg="green")
        else:
            typer.secho(f"  ✗ {name:<12} binary {binary!r} not on PATH", fg="yellow")
    typer.secho("config:", bold=True)
    if (repo / "adw.yaml").is_file():
        typer.secho(f"  ✓ {repo / 'adw.yaml'} valid", fg="green")
    else:
        typer.secho(f"  ✗ no adw.yaml in {repo} (using defaults — no gates!)", fg="red")
        failures += 1
    typer.secho("agent roles:", bold=True)
    for role in ("scout", "plan", "build", "review"):
        ra = config.resolve_role(role)
        marker = "✓" if shutil.which(config.backends.for_backend(ra.backend).binary) else "✗"
        typer.echo(f"  {marker} {role:<8} -> {ra.backend} / {ra.model or 'default model'}")
    typer.secho("gates:", bold=True)
    if not config.gates:
        typer.secho("  ✗ none configured", fg="red")
        failures += 1
    for name in config.gate_order():
        gate = config.gates.get(name)
        if gate is None:
            typer.secho(f"  ✗ {name}: in gate_order but not defined", fg="red")
            failures += 1
            continue
        typer.echo(f"  · {name:<10} $ {gate.command}")
    typer.secho("git:", bold=True)
    from adw.nodes import git_ops

    if git_ops.is_git_repo(repo):
        typer.secho(f"  ✓ {repo} is a git repo (branch {git_ops.current_branch(repo)})", fg="green")
    else:
        typer.secho(f"  ✗ {repo} is not a git repo", fg="red")
        failures += 1
    if config.ship.create_pr and not shutil.which("gh"):
        typer.secho("  ✗ ship.create_pr is on but `gh` is not installed", fg="red")
        failures += 1
    typer.secho("isolation:", bold=True)
    iso = config.isolation
    typer.echo(f"  type: {iso.type}")
    if iso.type == "container":
        if shutil.which(iso.binary):
            typer.secho(f"  ✓ {iso.binary} present (image: {iso.image})", fg="green")
        else:
            typer.secho(f"  ✗ {iso.binary} not found — install Apple's `container`", fg="red")
            failures += 1
        for secret in iso.secrets:
            if os.environ.get(secret):
                typer.secho(f"  ✓ secret {secret} set", fg="green")
            else:
                typer.secho(f"  ✗ secret {secret} missing from env", fg="red")
                failures += 1
    typer.secho("notify:", bold=True)
    typer.echo(f"  macos: {config.notify.macos}")
    typer.echo(f"  webhook: {config.notify.webhook or 'none'}")
    raise typer.Exit(1 if failures else 0)


@app.command()
def init(
    repo: Path = REPO_OPT,
    force: bool = typer.Option(False, "--force", help="Overwrite an existing adw.yaml"),
) -> None:
    """Inspect the project and generate a starter adw.yaml."""
    from adw import scaffold
    from adw.config import REPO_CONFIG_NAME
    from adw.nodes import git_ops

    target = repo / REPO_CONFIG_NAME
    if target.exists() and not force:
        typer.secho(f"{target} already exists (use --force to overwrite)", fg="red")
        raise typer.Exit(1)

    profile = scaffold.detect_project(repo)
    backend = scaffold.detect_backend(AdwConfig())
    text = scaffold.render_config(profile, backend)
    scaffold.validate_rendered(text)
    target.write_text(text)

    typer.secho(f"created {target} (ecosystem: {profile.ecosystem})", fg="green", bold=True)
    for name, gate in profile.gates.items():
        typer.echo(f"  {name:<10} $ {gate.command}")
    for note in profile.notes:
        typer.secho(f"  ! {note}", fg="yellow")
    if not git_ops.is_git_repo(repo):
        typer.secho(f"  ! {repo} is not a git repo — adw workflows need one", fg="yellow")
    typer.echo('next: adw doctor && adw run feature "..."')


@ticket_app.command("new")
def ticket_new(
    title: str = typer.Argument(help="Ticket title"),
    workflow: str = typer.Option("feature", help="Workflow, or 'auto' to route at process time"),
    priority: int = typer.Option(ticket_mod.DEFAULT_PRIORITY, help="Lower runs sooner"),
    body: str = typer.Option("", help="Ticket body (plain language task description)"),
    edit: bool = typer.Option(False, help="Open the ticket in $EDITOR after creating"),
    blocked_by: list[str] = BLOCKED_BY_OPT,
    repo: Path = REPO_OPT,
) -> None:
    """Create a ticket in the queue."""
    if workflow != "auto":
        get_workflow(workflow)  # validate name early ('auto' resolves at process time)
    if blocked_by:
        known = {t.id for state in ticket_mod.STATES for t in ticket_mod.list_tickets(repo, state)}
        for stem in blocked_by:
            if stem not in known:
                typer.secho(f"blocker '{stem}' does not match any existing ticket", fg="yellow")
    path = ticket_mod.write_ticket(
        repo, title, body, workflow=workflow, priority=priority, blocked_by=blocked_by or None
    )
    typer.echo(f"created {path}")
    if edit:
        subprocess.run([os.environ.get("EDITOR", "vi"), str(path)])


@ticket_app.command("edit")
def ticket_edit(
    ref: str = typer.Argument(help="Ticket id or unique substring of stem/title"),
    repo: Path = REPO_OPT,
) -> None:
    """Open a ticket in $EDITOR."""
    try:
        ticket = ticket_mod.find_ticket(repo, ref)
    except ticket_mod.TicketError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    subprocess.run([os.environ.get("EDITOR", "vi"), str(ticket.path)])


@ticket_app.command("rm")
def ticket_rm(
    ref: str = typer.Argument(help="Ticket id or unique substring of stem/title"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    repo: Path = REPO_OPT,
) -> None:
    """Delete a ticket."""
    try:
        ticket = ticket_mod.find_ticket(repo, ref)
    except ticket_mod.TicketError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    if not yes:
        typer.confirm(f"delete {ticket.path.name}?", abort=True)
    ticket_mod.remove(ticket)
    typer.echo(f"deleted {ticket.path}")


@ticket_app.command("bump")
def ticket_bump(
    ref: str = typer.Argument(help="Ticket id or unique substring of stem/title"),
    priority: int = typer.Option(..., help="Lower runs sooner"),
    repo: Path = REPO_OPT,
) -> None:
    """Change a ticket's priority."""
    try:
        ticket = ticket_mod.find_ticket(repo, ref)
    except ticket_mod.TicketError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    ticket_mod.set_priority(ticket, priority)
    typer.echo(f"set priority {priority} on {ticket.path.name}")


@ticket_app.command("requeue")
def ticket_requeue(
    ref: str = typer.Argument(help="Ticket id or unique substring of stem/title"),
    repo: Path = REPO_OPT,
) -> None:
    """Move a failed or done ticket back to the queue."""
    try:
        ticket = ticket_mod.find_ticket(repo, ref, ("failed", "done"))
    except ticket_mod.TicketError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    path = ticket_mod.requeue(repo, ticket)
    typer.echo(f"requeued {ticket.title} -> {path}")


@queue_app.command("list")
def queue_list(
    repo: Path = REPO_OPT,
    json_output: bool = typer.Option(False, "--json", help="Print all tickets as JSON."),
) -> None:
    """Show tickets in every state."""
    if json_output:
        typer.echo(
            json.dumps(
                {
                    state: [
                        {"workflow": t.workflow, "title": t.title, "priority": t.priority}
                        for t in ticket_mod.list_tickets(repo, state)
                    ]
                    for state in ticket_mod.STATES
                },
                indent=2,
            )
        )
        return
    done = ticket_mod.done_stems(repo)
    for state in ticket_mod.STATES:
        entries = ticket_mod.list_tickets(repo, state)
        typer.secho(f"{state} ({len(entries)})", bold=True)
        for ticket in entries:
            line = f"  p{ticket.priority} [{ticket.workflow}] {ticket.title}"
            if state == "queue":
                pending = ticket_mod.pending_blockers(ticket, done)
                if pending:
                    line += f"  [blocked by: {', '.join(pending)}]"
            typer.echo(line)


def _process_ticket(
    ticket: ticket_mod.Ticket, repo: Path, auto_approve_plan: bool, yes: bool
) -> rs.RunState:
    target_repo = ticket.repo or repo
    typer.secho(f"● ticket: {ticket.title} [{ticket.workflow}] -> {target_repo}", bold=True)
    config = _load(target_repo)
    workflow_name, task = ticket.workflow, ticket.task
    if workflow_name == "auto":
        routed = routing.route(task, config, target_repo)
        workflow_name, task = routed.workflow, routed.task
        typer.secho(f"  routed → {workflow_name} ({routed.method}): {routed.rationale}", fg="cyan")
    state = _execute(
        workflow_name,
        task,
        target_repo,
        config,
        auto_approve_plan,
        yes,
        source_ticket_run=ticket.source_run,
    )
    ticket_mod.finish(ticket, repo, state.status, state.outcome_detail, state.run_id)
    return state


@queue_app.command("process")
def queue_process(
    repo: Path = REPO_OPT,
    all_tickets: bool = typer.Option(False, "--all", help="Process until the queue is empty"),
    parallel: int = typer.Option(1, help="Run N tickets concurrently (needs worktree isolation)"),
    auto_approve_plan: bool = typer.Option(False, help="Skip engineer gate 1 (plan approval)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip BOTH engineer gates"),
) -> None:
    """Claim the next ticket (or all) and run its workflow."""
    if parallel > 1:
        if not yes:
            typer.secho("--parallel requires -y (gates can't be answered concurrently)", fg="red")
            raise typer.Exit(2)
        if _load(repo).isolation.type == "local":
            typer.secho(
                "--parallel needs isolation.type: worktree (or container) so runs don't collide",
                fg="red",
            )
            raise typer.Exit(2)
        _process_parallel(repo, parallel, auto_approve_plan, yes)
        return

    processed = 0
    while True:
        try:
            ticket = ticket_mod.claim_next(repo)
        except ticket_mod.TicketError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1) from exc
        if ticket is None:
            remaining = ticket_mod.list_tickets(repo, "queue")
            if remaining:
                done = ticket_mod.done_stems(repo)
                typer.secho(
                    f"{len(remaining)} ticket(s) remain blocked on unfinished blockers:",
                    fg="yellow",
                )
                for t in remaining:
                    typer.echo(f"  {t.id} <- {', '.join(ticket_mod.pending_blockers(t, done))}")
            elif processed == 0:
                typer.echo("queue is empty")
            break
        _process_ticket(ticket, repo, auto_approve_plan, yes)
        processed += 1
        if not all_tickets:
            break


@queue_app.command("watch")
def queue_watch(
    repo: Path = REPO_OPT,
    parallel: int = typer.Option(1, min=1, help="Run up to N tickets concurrently"),
    interval: float = typer.Option(5.0, min=0.1, help="Seconds between queue polls"),
    auto_approve_plan: bool = typer.Option(False, help="Skip engineer gate 1 (plan approval)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip BOTH engineer gates"),
) -> None:
    """Watch the queue and process tickets as they appear (Ctrl-C to stop)."""
    if _load(repo).isolation.type == "local":
        typer.secho(
            "queue watch needs isolation.type: worktree (or container) so runs don't collide",
            fg="red",
        )
        raise typer.Exit(2)
    if parallel > 1 and not yes:
        typer.secho("--parallel requires -y (gates can't be answered concurrently)", fg="red")
        raise typer.Exit(2)

    ticket_mod.ensure_dirs(repo)
    stop = threading.Event()

    def _shutdown(signum: int, frame: object) -> None:
        typer.secho(
            f"\n■ {signal.Signals(signum).name}: finishing in-flight runs, no new claims",
            fg="yellow", bold=True,
        )
        stop.set()

    previous = {sig: signal.signal(sig, _shutdown) for sig in (signal.SIGINT, signal.SIGTERM)}
    queue_dir = ticket_mod.tickets_root(repo) / "queue"
    typer.secho(
        f"▶ watching {queue_dir} every {interval:g}s (parallel={parallel}) — Ctrl-C to stop",
        bold=True,
    )
    try:
        results = _watch_loop(repo, parallel, interval, auto_approve_plan, yes, stop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    typer.secho(f"\nprocessed {len(results)} tickets:", bold=True)
    for title, status in results:
        typer.secho(f"  {status:<9} {title}", fg=_STATUS_COLOR.get(status, "white"))


@queue_app.command("retry")
def queue_retry(
    ticket_ref: str = typer.Argument(None, metavar="TICKET"),
    all_tickets: bool = typer.Option(False, "--all", help="Re-queue every failed ticket"),
    repo: Path = REPO_OPT,
) -> None:
    """Re-queue one or all failed tickets so the queue processes them again."""
    if bool(ticket_ref) == all_tickets:
        typer.secho("pass exactly one of TICKET or --all", fg="red")
        raise typer.Exit(2)

    if all_tickets:
        failed = ticket_mod.list_tickets(repo, "failed")
        if not failed:
            typer.echo("no failed tickets")
            return
        for ticket in failed:
            path = ticket_mod.requeue(repo, ticket)
            typer.echo(f"requeued {ticket.title} -> {path}")
        return

    try:
        ticket = ticket_mod.find_failed(repo, ticket_ref)
    except ticket_mod.TicketError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    path = ticket_mod.requeue(repo, ticket)
    typer.echo(f"requeued {ticket.title} -> {path}")


def _watch_loop(
    repo: Path,
    workers: int,
    interval: float,
    auto_approve_plan: bool,
    yes: bool,
    stop: threading.Event,
) -> list[tuple[str, str]]:
    """Poll the queue until `stop` is set; returns (title, status) per processed ticket."""
    from concurrent.futures import ThreadPoolExecutor

    results: list[tuple[str, str]] = []

    def worker() -> None:
        last_err: str | None = None
        while not stop.is_set():
            try:
                ticket = ticket_mod.claim_next(repo)  # atomic rename — safe across threads
            except ticket_mod.TicketError as exc:
                msg = str(exc)
                if msg != last_err:  # non-fatal: print once per distinct message, keep watching
                    typer.secho(msg, fg="red")
                    last_err = msg
                stop.wait(interval)
                continue
            if ticket is None:
                stop.wait(interval)
                continue
            try:
                state = _process_ticket(ticket, repo, auto_approve_plan, yes)
                status = state.status
            except Exception as exc:  # a bad ticket must not kill the watcher
                ticket_mod.finish(ticket, repo, "failed", f"watch error: {exc}", "")
                status = "failed"
            typer.secho(f"  {status:<9} {ticket.title}", fg=_STATUS_COLOR.get(status, "white"))
            results.append((ticket.title, status))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(worker) for _ in range(workers)]:
            future.result()
    return results


def _process_parallel(repo: Path, workers: int, auto_approve_plan: bool, yes: bool) -> None:
    from concurrent.futures import ThreadPoolExecutor

    results: list[tuple[str, str]] = []
    errors: list[str] = []

    def worker() -> None:
        while True:
            try:
                ticket = ticket_mod.claim_next(repo)  # atomic rename — safe across threads
            except ticket_mod.TicketError as exc:
                errors.append(str(exc))
                return
            if ticket is None:
                queue_dir = ticket_mod.tickets_root(repo) / "queue"
                in_flight = ticket_mod.tickets_root(repo) / "in_progress"
                if not any(queue_dir.glob("*.md")):
                    return  # truly drained
                if not any(in_flight.glob("*.md")):
                    return  # remaining are blocked and nothing in flight can unblock them
                time.sleep(0.2)  # a sibling's in-flight ticket may unblock more
                continue
            state = _process_ticket(ticket, repo, auto_approve_plan, yes)
            results.append((ticket.title, state.status))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(worker) for _ in range(workers)]:
            future.result()

    if errors:
        typer.secho(errors[0], fg="red")
        raise typer.Exit(1)
    remaining = ticket_mod.list_tickets(repo, "queue")
    if remaining:
        done = ticket_mod.done_stems(repo)
        typer.secho(
            f"{len(remaining)} ticket(s) remain blocked on unfinished blockers:", fg="yellow"
        )
        for t in remaining:
            typer.echo(f"  {t.id} <- {', '.join(ticket_mod.pending_blockers(t, done))}")
    if not results:
        if not remaining:
            typer.echo("queue is empty")
        return
    typer.secho(f"\nprocessed {len(results)} tickets:", bold=True)
    for title, status in results:
        typer.secho(f"  {status:<9} {title}", fg=_STATUS_COLOR.get(status, "white"))


@sandbox_app.command("build")
def sandbox_build(
    repo: Path = REPO_OPT,
    image: str | None = typer.Option(None, help="Image tag (default: isolation.image)"),
) -> None:
    """Build the container image agents run inside (wraps `container build`)."""
    config = _load(repo)
    image = image or config.isolation.image
    binary = config.isolation.binary
    if not shutil.which(binary):
        typer.secho(f"{binary!r} not found — install Apple's `container`", fg="red")
        raise typer.Exit(1)
    dockerfile = repo / "sandbox" / "Dockerfile"  # repo-local override
    if not dockerfile.is_file():
        dockerfile = Path(__file__).parent / "sandbox" / "Dockerfile"  # packaged default
    typer.secho(f"building {image} from {dockerfile}", bold=True)
    proc = subprocess.run(
        [binary, "build", "-t", image, "-f", str(dockerfile), str(dockerfile.parent)]
    )
    if proc.returncode == 0:
        typer.secho(f"✓ built {image}", fg="green")
    raise typer.Exit(proc.returncode)


@app.command("_agent", hidden=True)
def debug_agent(
    prompt: str = typer.Argument(help="Prompt to send"),
    backend: str = typer.Option("claude-code", help="Backend to exercise"),
    model: str = typer.Option(None, help="Model override"),
    session: str = typer.Option(None, help="Resume this session id"),
    read_only: bool = typer.Option(False),
    repo: Path = REPO_OPT,
) -> None:
    """Debug: one raw agent round trip (proves invoke/resume against a real CLI)."""
    config = _load(repo)
    adapter = get_adapter(backend, config)
    result = adapter.invoke(
        AgentInvocation(
            prompt=prompt, cwd=repo, model=model, session_id=session, read_only=read_only
        )
    )
    typer.echo(json.dumps(result.to_artifact(), indent=2, default=str))
    raise typer.Exit(0 if result.ok else 1)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"adw {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", help="Show version and exit", callback=_version_callback, is_eager=True
    ),
) -> None:
    pass
