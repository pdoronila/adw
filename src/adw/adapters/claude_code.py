"""Claude Code backend: `claude -p ... --output-format json`.

Docs: https://code.claude.com/docs/en/headless
Resume requires the same cwd used at session start.
"""

from __future__ import annotations

import json

from adw.adapters.base import AgentAdapter, AgentInvocation, AgentResult
from adw.config import ClaudeCodeOpts


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude-code"

    opts: ClaudeCodeOpts

    def build_command(self, inv: AgentInvocation) -> list[str]:
        opts = self.opts
        cmd = [opts.binary, "-p", inv.prompt, "--output-format", "json"]
        if inv.model:
            cmd += ["--model", inv.model]
        if inv.session_id:
            cmd += ["--resume", inv.session_id]
        if inv.read_only:
            cmd += ["--allowedTools", opts.readonly_tools]
        else:
            cmd += ["--permission-mode", opts.permission_mode]
            cmd += ["--allowedTools", opts.allowed_tools]
            if opts.dangerous:
                cmd += ["--dangerously-skip-permissions"]
        if opts.bare:
            cmd += ["--bare"]
        cmd += opts.extra_args
        return cmd

    def parse_output(self, stdout: str, stderr: str, exit_code: int) -> AgentResult:
        payload = None
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            pass
        if not isinstance(payload, dict):
            return AgentResult(
                ok=False,
                output=stdout,
                session_id=None,
                exit_code=exit_code,
                duration_s=0.0,
                stderr_tail=stderr[-2000:],
                error="could not parse claude JSON output",
            )
        is_error = bool(payload.get("is_error"))
        return AgentResult(
            ok=exit_code == 0 and not is_error,
            output=str(payload.get("result", "")),
            session_id=payload.get("session_id"),
            exit_code=exit_code,
            duration_s=0.0,
            cost_usd=payload.get("total_cost_usd"),
            raw=payload,
            stderr_tail=stderr[-2000:],
            error="claude reported is_error" if is_error else "",
        )
