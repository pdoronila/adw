#!/usr/bin/env python3
"""A fake `claude` binary for the e2e smoke test.

Speaks the claude-code headless protocol: called as
`fake_agent.py -p "<prompt>" --output-format json [...]`, prints one JSON
object with result/session_id/is_error. Behavior keys off the prompt text
(which of adw's templates it received) and actually edits files so the
deterministic gates have something real to measure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    prompt = args[args.index("-p") + 1] if "-p" in args else ""
    resumed = "--resume" in args

    if "scout the codebase" in prompt:
        result = "## Relevant files\n- feature.txt (to be created)\n- marker.txt (gate target)"
    elif "create an implementation plan" in prompt:
        result = "# Plan\n\n1. Create feature.txt containing 'hello'.\n2. Create marker.txt."
    elif "implement the approved plan" in prompt:
        # Deliberately do HALF the job: the gate (test -f marker.txt) must fail
        # so the workflow exercises the fix loop.
        Path("feature.txt").write_text("hello\n")
        result = "Created feature.txt. (forgot marker.txt)"
    elif "Validation gates failed" in prompt:
        if not resumed:
            print(
                json.dumps(
                    {
                        "is_error": True,
                        "result": "fix prompt arrived without --resume",
                        "session_id": "fake-sess-1",
                    }
                )
            )
            return 0
        Path("marker.txt").write_text("present\n")
        result = "Created marker.txt; gates should pass now."
    elif "review the changes" in prompt:
        result = "VERDICT: ship\n\nChange matches the plan."
    else:
        result = "echo: " + prompt[:80]

    print(
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": result,
                "session_id": "fake-sess-1",
                "total_cost_usd": 0.01,
                "num_turns": 1,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
