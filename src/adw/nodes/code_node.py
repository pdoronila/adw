"""Deterministic gate nodes: lint, typecheck, test — plain subprocesses, zero tokens."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from adw.config import GateConfig
from adw.exec_env import ExecutionEnvironment, LocalEnv

HEAD_CHARS = 2_000
TAIL_CHARS = 8_000


@dataclass
class GateResult:
    name: str
    command: str
    ok: bool
    exit_code: int
    output_excerpt: str
    log_path: Path
    duration_s: float


def truncate_middle(text: str, head: int = HEAD_CHARS, tail: int = TAIL_CHARS) -> str:
    """Keep the head and tail of long output; pytest/mypy put the signal at the end."""
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…[{omitted} chars truncated]…\n{text[-tail:]}"


def run_gate(
    name: str,
    cfg: GateConfig,
    cwd: Path,
    log_dir: Path,
    attempt: int,
    env: ExecutionEnvironment | None = None,
) -> GateResult:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"attempt-{attempt}-{name}.log"
    env = env or LocalEnv()
    start = time.monotonic()
    try:
        proc = env.run_shell(cfg.command, cwd=cwd, timeout=cfg.timeout)
        combined = proc.stdout + (("\n--- stderr ---\n" + proc.stderr) if proc.stderr else "")
        exit_code = proc.returncode
        ok = exit_code == 0
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        combined = f"{stdout}\n[gate timed out after {cfg.timeout}s]"
        exit_code = -1
        ok = False
    log_path.write_text(combined)
    return GateResult(
        name=name,
        command=cfg.command,
        ok=ok,
        exit_code=exit_code,
        output_excerpt=truncate_middle(combined),
        log_path=log_path,
        duration_s=time.monotonic() - start,
    )


def run_gates(
    order: list[str],
    gates: dict[str, GateConfig],
    cwd: Path,
    log_dir: Path,
    attempt: int,
    env: ExecutionEnvironment | None = None,
) -> list[GateResult]:
    """Run every configured gate (no fail-fast) so one fix prompt carries full signal."""
    return [run_gate(name, gates[name], cwd, log_dir, attempt, env) for name in order]
