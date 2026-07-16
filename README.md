# adw — AI Developer Workflows

**The agentic layer.** `adw` composes the three actors of value creation — **engineers, agents, and code** — into repeatable developer workflows: deterministic Python orchestrates headless coding agents, validation gates (lint / typecheck / test) route failures back into the build agent's *same session* until they pass, and you the engineer show up only at the two ends: plan approval and final review.

Backend-agnostic: works with **Claude Code**, **Codex CLI**, and **opencode**, interchangeable per role.

Built from the thesis in IndieDevDan's video [*FORGET Loop Engineering. Agentic Engineering is about THIS*](https://www.youtube.com/watch?v=VQy50fuxI34) — AI developer workflows over "loops". A visual walkthrough of how adw works: **https://pdoronila.github.io/adw/**

## Install (use it)

`adw` is a normal Python package with an `adw` console script, so coworkers install it straight from the private repo — no clone, no `uv sync`, and uv is not required on their machine (any of `uv tool`, `pipx`, or `pip` works). The prompt templates ship inside the wheel.

**Recommended — `uv tool install` from the private repo over SSH** (devs already have SSH keys for enterprise GitHub):

```bash
uv tool install git+ssh://git@github.<your-enterprise>/<org>/adw.git
# pin to a released tag instead of the default branch:
uv tool install git+ssh://git@github.<your-enterprise>/<org>/adw.git@v0.1.0

adw --version
uv tool update-shell     # once, if the tools bin dir isn't on PATH yet
```

Upgrade / remove: `uv tool upgrade adw` · `uv tool uninstall adw`.

**HTTPS instead of SSH** (needs a PAT with read access to the repo):

```bash
uv tool install "git+https://<token>@github.<your-enterprise>/<org>/adw.git@v0.1.0"
```

**pipx / pip equivalents** (same URL forms):

```bash
pipx install "git+ssh://git@github.<your-enterprise>/<org>/adw.git@v0.1.0"
pip  install "git+ssh://git@github.<your-enterprise>/<org>/adw.git@v0.1.0"
```

Cutting a release so installs can pin a version: `git tag v0.1.0 && git push origin v0.1.0` (optionally attach a GitHub Release).

### Runtime prerequisites

The Python deps (typer, pydantic, pyyaml) install automatically. Separately, each user needs on their PATH: `git`, at least one agent CLI (`claude`, `codex`, or `opencode` — whichever your `adw.yaml` roles use), and `gh` only if `ship.create_pr` is on. Run `adw doctor` in a target repo to check all of this.

### Distributing as versioned wheels instead of git installs

If you'd rather not grant repo read access to every consumer, build wheels and publish to an internal index (GitHub Packages / Artifactory / internal PyPI):

```bash
uv build                                   # -> dist/adw-<v>-py3-none-any.whl
uv publish --index <your-internal-index>   # or: twine upload -r <repo> dist/*
```

Consumers then `uv tool install adw --index <your-internal-index>` (or set `pip`'s `--index-url`). This adds auth/CI overhead; the git install above is the lighter default for an internal tool.

## Develop it

```bash
uv sync
uv run adw --help
uv run pytest && uv run ruff check . && uv run mypy
```

## Point it at a repo

Drop an `adw.yaml` into any target repository — or run `adw init` to inspect the project and generate the starter file shown below:

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
  max_review_iterations: 2
  gate_order: [lint, typecheck, test]

ship:
  branch_prefix: "adw/"
  create_pr: false
```

Model strings are backend-native and passed through verbatim (`opus` for claude, `gpt-5-codex` for codex, `provider/model` for opencode). Global defaults live in `~/.config/adw/config.yaml`; the repo file wins.

### Agent experts

An **expert** is reusable specialized instructions prepended to a role's prompt — the way to template your engineering into an agent (the video's "surgical hotfix agent"). Drop `experts/<name>.md` in the target repo and attach it to a role, optionally scoped to one workflow with a `<workflow>:<role>` key:

```yaml
agents:
  roles:
    "hotfix:build": {backend: claude-code, model: sonnet, expert: hotfix-surgeon}
```

Now the `build` agent in the `hotfix` workflow (and only there) carries the `hotfix-surgeon` instructions. See `examples/hotfix/` for a complete one. Experts can also be defined inline under an `experts:` map.

## Run a workflow

```bash
adw init                                # inspect the project and generate adw.yaml
adw doctor                              # check backends, config, gates
adw workflows                           # list available workflows
adw run feature "Add a --json flag to the export command"
adw run feature "..." --dry-run         # show resolved roles/gates, run nothing
adw run feature "..." -y                # unattended: skip both engineer gates
adw run feature "..." --model opus              # override the model for every role, this run only
adw run feature "..." --backend codex            # override the backend for every role, this run only
adw run feature "..." --isolation worktree       # override isolation.type for this run
adw status                              # list runs; adw status <run-id> for detail
```

### Available workflows

Every workflow is composed from the same reusable steps (`src/adw/workflows/steps.py`); they differ in which agents run and where the human gates sit. Assign model tiers per role in `adw.yaml` (e.g. a cheap workhorse for `chore`, a SOTA model for `plan`/`research`). `--model`/`--backend` on `adw run` override this uniformly for a single run.

| Workflow | Shape | For |
|---|---|---|
| `feature` | scout → plan → **approve** → build → gate loop → review → **ship** | new functionality |
| `bug` | scout → diagnose → **approve** → fix + regression test → gate loop → review → **ship** | defects; ships a test that fails before, passes after |
| `chore` | build → gate loop → **ship** (one workhorse agent, no plan/review) | small low-risk work (dep bumps, renames) |
| `hotfix` | scout → **approve fix** → implement → gate loop → **ship ASAP** | production incidents; human signs off on the approach up front |
| `cve` | research → **approve** → reproduce (failing test) → mitigate → gate loop → security review → **ship** | reproduce a vulnerability in your own repo, then build + regression-guard the protection |

`feature` and `bug` split reconnaissance from planning: a read-only **scout** agent surveys the codebase first (relevant files, patterns to reuse, tests, constraints) and its findings feed the planner — a cheap way to raise plan quality.

**Bold** = an engineer gate. Examples:

```bash
adw run chore  "Bump ruff to the latest 0.x and fix any new lint"
adw run bug    "search() returns duplicates when the query has trailing whitespace"
adw run hotfix "Checkout 500s: NPE in PaymentService.total() when cart is empty"
adw run cve    "CVE-2024-XXXX: path traversal in read_document() — reproduce '../secret' escaping DOCS_ROOT, then confine reads to it"
```

The **cve** workflow is defensive: it reproduces the flaw as a security test *inside your own test suite* (the test fails against the vulnerable code, proving the hole), implements the protection so the test passes, and leaves that test behind as a permanent regression guard. Reproduction is confined to your repo — no external targeting.

What `adw run feature` does:

1. **Preflight** *(code)* — git repo, clean tree, gates configured
2. **Branch** *(code)* — `adw/<run-id>`
3. **Plan** *(agent, read-only)* — writes `plan.md`
4. **Engineer gate 1** — approve / edit in `$EDITOR` / reject
5. **Build** *(agent)* — implements the plan; session id is persisted
6. **Gate loop** *(code)* — run all gates; on failure, feed excerpts back into the **same build session**; repeat up to `max_fix_iterations`
7. **Review loop** *(agent, fresh session each round, read-only)* — reviews the diff; `VERDICT: concerns` routes the findings back into the **build session** (revise), re-runs the gate loop, and re-reviews, up to `max_review_iterations` rounds; then proceeds to the final gate either way
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

## Factory router

Don't want to pick the workflow by hand? The router classifies a request and chooses one:

```bash
adw route "checkout 500s in prod on empty cart"     # → hotfix
adw route "bump pydantic and fix breakages"          # → chore
adw route "add CSV export to reports"                # → feature
adw route "..." --run -y                             # classify AND run it
```

A read-only agent glances at the repo and returns the workflow + a refined task; if the agent is unavailable or returns junk, it falls back to deterministic keyword matching, so routing never hard-fails. File a ticket with `--workflow auto` and `queue process` routes it at claim time:

```bash
adw ticket new "login is broken for SSO users" --workflow auto
adw queue process        # → routed → bug (agent): ...
```

## Async runs (pause & resume)

By default the engineer gates block the terminal. With `--async`, a run **pauses** at each gate and persists its state instead, so you can approve later (or from elsewhere):

```bash
adw run feature "add CSV export" --async     # runs scout+plan, then pauses
# ■ paused: awaiting plan approval
#   ↳ adw resume 20260716-... --approve   (or --reject)

adw resume 20260716-... --approve            # build → gates → review, pauses at final
adw resume 20260716-... --approve            # ships
adw resume 20260716-... --edit               # edit the plan/diff, then approve
```

Resuming replays the workflow from its checkpoint: completed steps are skipped (no agent re-invocation — the plan, build session, and gate results are all persisted), and only the pending gate advances. This is what lets the queue run many tickets without a human babysitting each one.

## Isolation, parallelism & racing

By default a run works on a branch in the main tree (`isolation.type: local`). Set `isolation.type: worktree` and each run gets its own **git worktree** — so runs don't trip over each other and can go concurrently:

```yaml
isolation:
  type: worktree            # local | worktree | container
```

- **Parallel queue** — drain the queue N-at-a-time (each ticket in its own worktree):
  ```bash
  adw queue process --all --parallel 4 -y
  ```
- **Racing** — spin up N candidates for one task; the first to pass gates wins, the losers' branches are dropped (the video's "3, 5, 10 agents racing, fastest wins"):
  ```bash
  adw run hotfix "checkout 500s on empty cart" --race 3
  ```

Both need `isolation: worktree` (or `container`) and run unattended (`-y`). Shipped worktrees are cleaned up automatically; a failed run's worktree is kept for salvage.

### Container sandboxes (Apple `container`)

`isolation.type: container` runs each agent and gate inside an [Apple container](https://github.com/apple/container) — a lightweight Linux VM per invocation (macOS 26; macOS 15 with limits). Git orchestration stays on the host; only the untrusted agent + gate commands enter the sandbox.

```yaml
isolation:
  type: container
  image: adw-sandbox
  secrets: [ANTHROPIC_API_KEY]   # forwarded into the container as -e
```

```bash
adw sandbox build          # build the image (git + node + agent CLIs)
export ANTHROPIC_API_KEY=sk-ant-...
adw run chore "..." --repo . 
```

Container isolation composes with a per-run **worktree** (git state stays isolated per run), so `--parallel N` and `--race N` work with containers too — each candidate is its own VM mounting its own worktree.

**Auth** is the key detail: the macOS keychain doesn't cross into a Linux VM, so there's no interactive login inside the container. Instead adw forwards the named `secrets` as `-e NAME` (only those actually set on the host), and in `-p` mode Claude Code authenticates directly. For a **Max/Pro subscription**, run `claude setup-token` once and `export CLAUDE_CODE_OAUTH_TOKEN=…` — adw forwards it and usage bills your plan (no API charge). `ANTHROPIC_API_KEY` uses the API instead; `OPENAI_API_KEY` for codex; provider keys for opencode. `adw doctor` reports whether the `container` binary and each secret are present.

The image is defined by a Dockerfile shipped with adw (`adw sandbox build` uses it; drop a `sandbox/Dockerfile` in your repo to override). Add or remove agent CLIs there to match the backends your `adw.yaml` uses.

## Dogfooding adw on adw

adw builds features into its own repo — it's the fastest way to exercise the tool. Install it editable so runs pick up source changes with no reinstall (`uv tool install --editable .`), then work at the altitude the change deserves.

**One feature, reviewed at both gates.** Interactive runs block at the engineer gates; drive them with `--async` instead and answer each gate on your own time:

```bash
adw run feature "add a --json flag to adw status" --async   # runs scout+plan, then pauses
# review .adw/runs/<id>/plan.md, then:
adw resume <id> --approve                                    # build+gates+review, pauses at final
# review the diff (adw status <id>) + .adw/runs/<id>/review.md, then:
adw resume <id> --approve                                    # ships to branch adw/<id>
```

**A batch, unattended.** File tickets and run several at once, each in its own worktree (gates still enforce; there's no human gate in `--parallel`):

```yaml
# adw.yaml
isolation: {type: worktree}
```
```bash
adw ticket new "route --json flag"      --body "..."
adw ticket new "queue list --json flag" --body "..."
adw ticket new "workflows --json flag"  --body "..."
adw queue process --all --parallel 3 -y
```

**Sandboxed.** Set `isolation: {type: container}` and each run's agent + gates execute inside an Apple container (VM-level isolation, nothing touches your host) — composes with worktrees, so `--parallel` still works. Use it for untrusted changes or large fan-outs.

**It never touches `main`.** Every run commits on `adw/<run-id>` and stops. Review the branch (`git diff main..adw/<id>`) or, with `ship.create_pr: true` (+ a GitHub remote), let each run open its own PR. Merge only on approval; `git branch -D adw/<id>` to discard.

**Editing its own source is safe.** The running `adw` process loads its code once at start, so a run that rewrites `steps.py` won't destabilize itself mid-flight — the change applies on the next invocation.

## Design rules (from the thesis)

- **Separate code from agents.** Orchestration, gates, and git are plain code — fast, free, deterministic. Agents only do the fuzzy work (plan, build, fix, review).
- **Same session for fixes.** Gate failures resume the build agent's session so accumulated context is never thrown away.
- **Fresh session for review.** The reviewer must not share the builder's biases. When it raises concerns, those findings loop back into the build session for a revise round (re-gated, re-reviewed) before the human final gate.
- **Engineer at the ends.** Prompting/planning and reviewing/validation are the two constraints of agentic engineering; everything between them runs without you.

## Extending

- New workflow: compose one from the shared steps in `src/adw/workflows/steps.py` (`preflight`, `start_branch`, `agent_doc`, `approve_gate`, `build`, `resume_turn`, `gate_loop`, `review`, `review_loop`, `final_gate`, `ship`) — each returns `RunOutcome | None`, where non-None short-circuits. See `feature.py`/`cve.py` for the pattern; register it in `WORKFLOWS`.
- New backend: subclass `AgentAdapter` (`build_command` + `parse_output`), register in `ADAPTERS`, add a `BackendOpts` model.
- New prompt: drop a `<name>.md` in `src/adw/prompts/` and render it with `prompts.render(name, **kwargs)`.

## Development

```bash
uv run pytest          # unit + e2e smoke (uses a fake agent, no tokens)
uv run ruff check .
uv run mypy
```
