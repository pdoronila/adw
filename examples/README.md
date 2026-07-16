# adw examples

One ready-to-run seed repo per workflow. `scaffold.py` copies an example into a
fresh git repo (default `/tmp/adw-examples/<name>`) and optionally runs the
workflow against it — so you can exercise adw end to end without hand-building
fixtures.

```bash
python examples/scaffold.py list            # see all examples
python examples/scaffold.py setup cve       # seed a repo, print the run command
python examples/scaffold.py run   chore -y  # seed and run, skipping the engineer gates
```

`run` without `-y` stops at the real engineer gates (plan approval, final review).
Drop `-y` to drive them yourself. Override the destination with `--dest DIR` or
the `ADW_EXAMPLES_DIR` env var.

Each `examples/<name>/` directory is exactly the seed repo (source, tests,
`adw.yaml`) plus an `example.json` holding the workflow and task. A `.gitignore`
is added automatically when the repo is materialized.

| Example | Workflow | What it exercises |
|---|---|---|
| `feature` | feature | plan → approve → build → gate → review → ship (adds `multiply` + test) |
| `chore`   | chore   | single workhorse agent, no plan/review (adds type hints) |
| `bug`     | bug     | diagnose a real double-append duplicate, fix with a regression test |
| `hotfix`  | hotfix  | prod-down `ZeroDivisionError` on empty cart; minimal human-approved fix |
| `cve`     | cve     | reproduce a path-traversal as a failing security test, then build the protection |

The seed tests pass on the un-modified code, so preflight and the first gate are
green — each workflow then does its own work on top. For `bug` and `cve`, verify
the added test genuinely fails without the fix: after a run, `git revert` the fix
hunk (or temporarily restore the seed source) and re-run the new test.

Models: `feature`/`chore` use `haiku` (cheap); `bug`/`hotfix`/`cve` use `sonnet`.
Edit the example's `adw.yaml` to point at another backend (`codex`, `opencode`)
or model.
