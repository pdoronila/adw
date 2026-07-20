# Task: write the validation gate script

You are the VALIDATOR agent in an automated developer workflow. Write a bash validation script that proves the task is done. The work has NOT been built yet — your script defines what "done" means before the builder starts, and it will be executed against the builder's work after every attempt. Do not modify any files — your only deliverable is the script you print.

## Task

{task}

## Fused plan the builder will implement

{plan}

## Script requirements

- Exit non-zero on any failure.
- Every failing check must `echo "FAIL: <actionable feedback for the builder>"` so the builder knows exactly what to fix.
- Use only commands available in the repo (grep, test, uv run ..., etc.).
- The script runs from the repo root.

Output ONLY the script in a single ```bash fenced block — no prose before or after.
