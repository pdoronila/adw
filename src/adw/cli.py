"""adw — AI Developer Workflows CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import typer

from adw import __version__, routing
from adw.adapters import ADAPTERS, get_adapter
from adw.adapters.base import AgentInvocation
from adw.config import AdwConfig, load_config
from adw.nodes.agent_node import AgentRunner
from adw.queue import tickets as ticket_mod
from adw.state import run_state as rs
from adw.workflows import WORKFLOWS, WorkflowContext, get_workflow

app = typer.Typer(
    help="AI Developer Workflows: code + agents + you, composed.", no_args_is_help=True
)
queue_app = typer.Typer(help="Process the file-based ticket queue.", no_args_is_help=True)
ticket_app = typer.Typer(help="Create and inspect tickets.", no_args_is_help=True)
app.add_typer(queue_app, name="queue")
app.add_typer(ticket_app, name="ticket")

REPO_OPT = typer.Option(Path("."), "--repo", help="Target repository", resolve_path=True)


def _load(repo: Path) -> AdwConfig:
    try:
        return load_config(repo)
    except Exception as exc:
        typer.secho(f"config error: {exc}", fg="red")
        raise typer.Exit(2) from exc


def _execute(
    workflow_name: str,
    task: str,
    repo: Path,
    config: AdwConfig,
    auto_approve_plan: bool,
    assume_yes: bool,
) -> rs.RunState:
    workflow = get_workflow(workflow_name)
    run_id = rs.new_run_id(task)
    run_dir = rs.create_run_dir(repo, run_id)
    state = rs.RunState(run_id=run_id, workflow=workflow_name, task=task, repo=str(repo))
    rs.save_state(state, run_dir)
    ctx = WorkflowContext(
        repo_dir=repo,
        run_dir=run_dir,
        config=config,
        state=state,
        task=task,
        agents=AgentRunner(config, run_dir, workflow=workflow_name),
        auto_approve_plan=auto_approve_plan,
        assume_yes=assume_yes,
    )
    typer.secho(f"▶ run {run_id} [{workflow_name}] in {repo}", bold=True)
    outcome = workflow.run(ctx)
    color = {"shipped": "green", "rejected": "yellow", "failed": "red"}[outcome.status]
    typer.secho(f"■ {outcome.status}: {outcome.reason}", fg=color, bold=True)
    for hint in outcome.hints:
        typer.echo(f"  ↳ {hint}")
    if state.total_cost_usd:
        typer.echo(f"  agent cost: ${state.total_cost_usd:.2f}")
    typer.echo(f"  artifacts: {run_dir}")
    return state


@app.command()
def run(
    workflow: str = typer.Argument(help="Workflow name (see `adw workflows`)"),
    task: str = typer.Argument(help="What to do, in plain language"),
    repo: Path = REPO_OPT,
    auto_approve_plan: bool = typer.Option(False, help="Skip engineer gate 1 (plan approval)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip BOTH engineer gates (unattended)"),
    max_iterations: int | None = typer.Option(None, help="Override workflow.max_fix_iterations"),
    dry_run: bool = typer.Option(False, help="Print the resolved plan of execution and exit"),
) -> None:
    """Run one AI developer workflow end to end."""
    config = _load(repo)
    if max_iterations is not None:
        config.workflow.max_fix_iterations = max_iterations
    if dry_run:
        _print_dry_run(workflow, task, repo, config)
        return
    state = _execute(workflow, task, repo, config, auto_approve_plan, yes)
    raise typer.Exit(0 if state.status == "shipped" else 1)


def _print_dry_run(workflow: str, task: str, repo: Path, config: AdwConfig) -> None:
    get_workflow(workflow)  # validate name
    typer.secho(f"workflow: {workflow}", bold=True)
    typer.echo(f"task: {task}")
    typer.echo(f"repo: {repo}")
    typer.echo("agent roles:")
    for role in ("scout", "plan", "build", "review"):
        ra = config.resolve_role(role)
        typer.echo(f"  {role:<8} -> {ra.backend} (model: {ra.model or 'backend default'})")
    typer.echo(f"gates (order): {', '.join(config.gate_order()) or '(none configured!)'}")
    for name in config.gate_order():
        gate = config.gates[name]
        typer.echo(f"  {name:<10} $ {gate.command}  (timeout {gate.timeout}s)")
    typer.echo(f"max fix iterations: {config.workflow.max_fix_iterations}")
    typer.echo(f"ship: branch_prefix={config.ship.branch_prefix} create_pr={config.ship.create_pr}")


@app.command()
def route(
    task: str = typer.Argument(help="Ticket / request in plain language"),
    repo: Path = REPO_OPT,
    run: bool = typer.Option(False, help="Run the chosen workflow instead of just printing it"),
    auto_approve_plan: bool = typer.Option(False, help="Skip engineer gate 1 when --run"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip both engineer gates when --run"),
) -> None:
    """Classify a request into the right workflow (the factory router)."""
    config = _load(repo)
    result = routing.route(task, config, repo)
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
def workflows() -> None:
    """List registered workflows."""
    for name, wf in sorted(WORKFLOWS.items()):
        typer.echo(f"{name:<10} {wf.description}")


@app.command()
def status(
    run_id: str = typer.Argument(None),
    repo: Path = REPO_OPT,
) -> None:
    """Show recent runs, or full detail for one run."""
    if run_id:
        run_dir = rs.runs_root(repo) / run_id
        if not (run_dir / "state.json").is_file():
            typer.secho(f"no run {run_id!r} under {rs.runs_root(repo)}", fg="red")
            raise typer.Exit(1)
        typer.echo(json.dumps(json.loads((run_dir / "state.json").read_text()), indent=2))
        return
    states = rs.list_runs(repo)
    if not states:
        typer.echo("no runs yet")
        return
    for state in states[-20:]:
        typer.echo(
            f"{state.run_id:<42} {state.workflow:<8} {state.status:<24} "
            f"${state.total_cost_usd:>6.2f}  {state.outcome_detail[:60]}"
        )


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
            version = subprocess.run(
                [binary, "--version"], capture_output=True, text=True
            ).stdout.strip().splitlines()
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
    repo: Path = REPO_OPT,
) -> None:
    """Create a ticket in the queue."""
    if workflow != "auto":
        get_workflow(workflow)  # validate name early ('auto' resolves at process time)
    path = ticket_mod.write_ticket(repo, title, body, workflow=workflow, priority=priority)
    typer.echo(f"created {path}")
    if edit:
        import os

        subprocess.run([os.environ.get("EDITOR", "vi"), str(path)])


@queue_app.command("list")
def queue_list(repo: Path = REPO_OPT) -> None:
    """Show tickets in every state."""
    for state in ticket_mod.STATES:
        entries = ticket_mod.list_tickets(repo, state)
        typer.secho(f"{state} ({len(entries)})", bold=True)
        for ticket in entries:
            typer.echo(f"  p{ticket.priority} [{ticket.workflow}] {ticket.title}")


@queue_app.command("process")
def queue_process(
    repo: Path = REPO_OPT,
    all_tickets: bool = typer.Option(False, "--all", help="Process until the queue is empty"),
    auto_approve_plan: bool = typer.Option(False, help="Skip engineer gate 1 (plan approval)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip BOTH engineer gates"),
) -> None:
    """Claim the next ticket (or all) and run its workflow."""
    processed = 0
    while True:
        ticket = ticket_mod.claim_next(repo)
        if ticket is None:
            if processed == 0:
                typer.echo("queue is empty")
            break
        target_repo = ticket.repo or repo
        typer.secho(f"● ticket: {ticket.title} [{ticket.workflow}] -> {target_repo}", bold=True)
        config = _load(target_repo)
        workflow_name, task = ticket.workflow, ticket.task
        if workflow_name == "auto":
            routed = routing.route(task, config, target_repo)
            workflow_name, task = routed.workflow, routed.task
            typer.secho(
                f"  routed → {workflow_name} ({routed.method}): {routed.rationale}", fg="cyan"
            )
        state = _execute(workflow_name, task, target_repo, config, auto_approve_plan, yes)
        ticket_mod.finish(ticket, repo, state.status, state.outcome_detail, state.run_id)
        processed += 1
        if not all_tickets:
            break


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
