# adw feature backlog

Tier 1 items are already filed as tickets (`adw queue list`). This file holds the
larger Tier 2/3 ideas, each with a ready-to-run command. Tier 2 items are
design-heavy: run them one at a time with `--async` so you review the plan at the
first gate before any code is written. Tier 3 items are bigger still — file them
with `adw ticket new` when ready, or run them `--async` and expect a couple of
review rounds.

## Tier 2 — high value, review the plan first

### 1. Diff view before approval

Show the run's actual change before the final gate: `adw status <id> --diff`
printing `git diff <base_branch>..<work_branch>`, and a "Changes" card on the run
detail page (collapsed per-file `<details>`, diff rendered read-only). The UI card
should only render when the work branch exists.

```bash
adw run feature "Add a diff view before approval: 'adw status <run-id> --diff' prints git diff base_branch..work_branch for the run (plain text, pipeable), and the web UI run-detail page (src/adw/ui/) gains a 'Changes' card showing the same diff with per-file collapsible <details> sections and added/removed line counts. Diff computation belongs in a shared read-only helper (e.g. src/adw/ui/views.py or a git helper module) reused by both CLI and UI. Handle missing branches gracefully (no card / clear CLI message). Tests for the helper and the UI route." --async
```

### 2. Cancel a running run

There is resume and retry but no abort. Kill the detached process, mark the run
`cancelled` (new terminal status), clean up per the isolation mode, keep the
branch for salvage.

```bash
adw run feature "Add 'adw cancel <run-id>': terminate a running run. Record the spawned process's pgid/pid in state.json when a run starts (see src/adw/ui/runner.py start_new_session usage and the CLI run path), send SIGTERM to the process group, set a new terminal status 'cancelled' in RunState (src/adw/state/run_state.py — update RunStatus literal, UI pill mapping in src/adw/ui/views.py PILL_STATUS, and the status views), and keep the work branch/worktree for salvage. Add a Cancel button on the web UI run-detail page for running runs (POST /runs/{id}/cancel, 303 redirect with a toast). Guard: cancelling a non-running run is a clear error. Tests: state transition, endpoint redirect, argv/signal via monkeypatch." --async
```

### 3. Auto-file failure tickets (self-healing loop)

When a run fails terminally, file a ticket so the failure itself enters the
queue: body carries the run id, `outcome_detail`, and the tail of the failing
gate log; workflow `auto` so the router classifies it at claim time.

```bash
adw run feature "When a run ends with status=failed, automatically create a ticket via the existing tickets module (src/adw/queue/tickets.py): title 'Investigate failed run <run-id>: <short outcome>', workflow 'auto', priority 3, body containing run id, workflow, task, outcome_detail, and the last ~40 lines of the failing gate log from .adw/runs/<id>/gates/. Make it opt-in via adw.yaml (e.g. queue.file_failures: true, default false) and never file a ticket for a run that itself came from such a ticket (guard against loops — mark provenance in the ticket frontmatter and/or RunState). Wire it at the point where failed status is persisted in the workflow steps (src/adw/workflows/steps.py). Tests: ticket created on failure when enabled, absent when disabled, loop guard holds." --async
```

### 4. Cost guardrails + reporting

A per-run budget that pauses the run when exceeded, plus visible cost rollups.

```bash
adw run feature "Add cost guardrails and reporting. (1) adw.yaml gains limits.max_cost_usd (optional float); after each agent invocation the workflow checks RunState.total_cost_usd and, if over budget, pauses the run with a new pending gate 'budget' (reuse the existing pause/resume machinery in src/adw/workflows/steps.py — engineer resumes with --approve to continue or --reject to stop). (2) 'adw status --costs' prints total and per-workflow cost rollups across runs. (3) The web UI sidebar shows total spend across runs (src/adw/ui/). Tests: over-budget pause triggers at the right boundary, resume continues, rollup math." --async
```

## Tier 3 — ambitious, human-in-the-loop

### 5. Queue watch daemon

`adw queue watch --parallel N` polls the queue and processes tickets as they
arrive — combined with notifications (Tier 1) and auto-filed failure tickets
(Tier 2 #3), adw becomes a standing factory.

```bash
adw run feature "Add 'adw queue watch [--parallel N] [--interval SECONDS]': a long-running foreground command that polls .adw/tickets/queue/ and claims/processes tickets as they appear, reusing the existing queue process machinery (src/adw/queue/) including --parallel worktree semantics. Clean shutdown on SIGINT/SIGTERM (finish in-flight runs, stop claiming). Refuses to start unless isolation is worktree or container. Status line output per claim/finish. Tests: poll loop claims a newly written ticket (short interval), respects parallel cap, shuts down cleanly." --async
```

### 6. GitHub Issues sync

Import labeled issues as tickets; post run results back to the issue.

```bash
adw run feature "GitHub Issues integration using the gh CLI (no new deps, degrade gracefully when gh or a remote is missing, mirroring how ship.create_pr degrades): (1) 'adw ticket import --gh --label adw' creates queue tickets from open issues with that label (title, body, issue number stored in ticket frontmatter as github_issue), skipping already-imported issue numbers. (2) When a run that originated from an imported ticket ships, comment on the issue with the run id, branch name, and PR URL if one was created. Config block github: {label: adw, comment: true} in adw.yaml. Tests with gh calls monkeypatched: import mapping, dedupe, comment payload, graceful degradation." --async
```
