"""Claude Code backend: `claude -p ... --output-format json`.

Docs: https://code.claude.com/docs/en/headless
Resume requires the same cwd used at session start.
"""

from __future__ import annotations

import json
from typing import Any

from adw.adapters.base import AgentAdapter, AgentInvocation, AgentResult, TokenUsage
from adw.config import ClaudeCodeOpts

# Claude usage keys -> TokenUsage fields. `usage` uses snake_case; `modelUsage`
# entries have appeared camelCased, so both spellings are accepted.
_USAGE_KEYS = {
    "input_tokens": ("input_tokens", "inputTokens"),
    "output_tokens": ("output_tokens", "outputTokens"),
    "cache_read_tokens": ("cache_read_input_tokens", "cacheReadInputTokens"),
    "cache_write_tokens": ("cache_creation_input_tokens", "cacheCreationInputTokens"),
}


def _usage_from(data: Any) -> TokenUsage | None:
    """Defensively map a claude usage dict to TokenUsage; None for anything else."""
    if not isinstance(data, dict):
        return None
    counts: dict[str, int] = {}
    for field, keys in _USAGE_KEYS.items():
        for key in keys:
            value = data.get(key)
            if isinstance(value, int | float) and value >= 0:
                counts[field] = int(value)
                break
    if not counts:
        return None
    return TokenUsage(**counts)


def _model_tokens_from(data: Any) -> dict[str, TokenUsage]:
    """Per-model usage from a `modelUsage` dict; malformed entries are skipped."""
    if not isinstance(data, dict):
        return {}
    out: dict[str, TokenUsage] = {}
    for model_id, entry in data.items():
        usage = _usage_from(entry)
        if isinstance(model_id, str) and usage is not None:
            out[model_id] = usage
    return out


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
            tokens=_usage_from(payload.get("usage")),
            model_tokens=_model_tokens_from(payload.get("modelUsage")),
            raw=payload,
            stderr_tail=stderr[-2000:],
            error="claude reported is_error" if is_error else "",
        )
