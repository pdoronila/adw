# adw — AI Developer Workflows

**The agentic layer.** `adw` composes the three actors of value creation — **engineers, agents, and code** — into repeatable developer workflows: deterministic Python orchestrates headless coding agents, validation gates (lint / typecheck / test) route failures back into the build agent's *same session* until they pass, and you the engineer show up only at the two ends: plan approval and final review.

Backend-agnostic: works with **Claude Code**, **Codex CLI**, and **opencode**, interchangeable per role.

## Install

```bash
uv sync
uv run adw --help          # or: uv tool install --editable . && adw --help
```

## Point it at a repo

Drop an `adw.yaml` into any target repository:

```yaml
gates:
  lint:      {command: "uv run ruff check .", timeout: 120}
  typecheck: {command: "uv run mypy src",     timeout: 300}
  test:      {command: "uv run pytest -x -q", timeout: 900}

agents:
  default: {backend: claude-code, model: sonnet}
  roles:
    plan:   {backend: claude-code, model: opus}     # SOTA model for planning
    build:  {backend: claude-code, model: sonnet}   # workhorse for building
    review: {backend: opencode, model: anthropic/claude-sonnet-4-5}

workflow:
  max_fix_iterations: 3
  gate_order: [lint, typecheck, test]

ship:
  branch_prefix: "adw/"
  create_pr: false
```

Model strings are backend-native and passed through verbatim (`opus` for claude, `gpt-5-codex` for codex, `provider/model` for opencode). Global defaults live in `~/.config/adw/config.yaml`; the repo file wins.

## Run a workflow

```bash
adw doctor                              # check backends, config, gates
adw run feature "Add a --json flag to the export command"
adw run feature "..." --dry-run         # show resolved roles/gates, run nothing
adw run feature "..." -y                # unattended: skip both engineer gates
adw status                              # list runs; adw status <run-id> for detail
```

What `adw run feature` does:

1. **Preflight** *(code)* — git repo, clean tree, gates configured
2. **Branch** *(code)* — `adw/<run-id>`
3. **Plan** *(agent, read-only)* — writes `plan.md`
4. **Engineer gate 1** — approve / edit in `$EDITOR` / reject
5. **Build** *(agent)* — implements the plan; session id is persisted
6. **Gate loop** *(code)* — run all gates; on failure, feed excerpts back into the **same build session**; repeat up to `max_fix_iterations`
7. **Review** *(agent, fresh session, read-only)* — advisory report on the diff
8. **Engineer gate 2** — diff summary + review → ship or reject
9. **Ship** *(code)* — commit on the work branch, optional `gh pr create`

Every run leaves a full artifact trail in the target repo under `.adw/runs/<run-id>/`: `state.json` (updated atomically at every step), `plan.md`, `review.md`, raw agent transcripts in `agent/`, full gate logs per attempt in `gates/`.

## Ticket queue

Work can also enter through a file-based queue (directory = state, renames are atomic):

```bash
adw ticket new "Fix flaky retry test" --workflow feature --priority 2 --edit
adw queue list
adw queue process            # claim highest-priority ticket, run its workflow
adw queue process --all -y   # drain the queue unattended
```

Tickets are markdown with YAML frontmatter in `.adw/tickets/queue/`; they move to `in_progress/`, then `done/` or `failed/` with a `## Result` section appended.

## Design rules (from the thesis)

- **Separate code from agents.** Orchestration, gates, and git are plain code — fast, free, deterministic. Agents only do the fuzzy work (plan, build, fix, review).
- **Same session for fixes.** Gate failures resume the build agent's session so accumulated context is never thrown away.
- **Fresh session for review.** The reviewer must not share the builder's biases.
- **Engineer at the ends.** Prompting/planning and reviewing/validation are the two constraints of agentic engineering; everything between them runs without you.

## Extending

- New workflow (chore, bug, hotfix…): implement the `Workflow` protocol in `src/adw/workflows/`, register it in `WORKFLOWS`.
- New backend: subclass `AgentAdapter` (`build_command` + `parse_output`), register in `ADAPTERS`, add a `BackendOpts` model.

## Development

```bash
uv run pytest          # unit + e2e smoke (uses a fake agent, no tokens)
uv run ruff check .
uv run mypy
```
