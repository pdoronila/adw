"""Agent adapter interface: one contract over every headless coding-agent CLI."""

from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from adw.config import BackendOpts
from adw.exec_env import ExecutionEnvironment, LocalEnv


@dataclass
class AgentInvocation:
    """Everything needed to run one agent turn."""

    prompt: str
    cwd: Path
    model: str | None = None
    session_id: str | None = None  # None -> new session; set -> resume
    read_only: bool = False
    timeout_s: int = 1800
    # Where the command runs (host or container). None -> host (LocalEnv).
    env: ExecutionEnvironment | None = None


@dataclass
class AgentResult:
    ok: bool
    output: str
    session_id: str | None
    exit_code: int
    duration_s: float
    cost_usd: float | None = None
    raw: Any = None
    stderr_tail: str = ""
    error: str = ""

    def to_artifact(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "session_id": self.session_id,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "cost_usd": self.cost_usd,
            "error": self.error,
            "stderr_tail": self.stderr_tail,
            "raw": self.raw,
        }


class AgentAdapter(ABC):
    """Subprocess wrapper for one agent CLI backend.

    build_command/parse_output are pure so each backend is unit-testable
    without spawning processes; invoke() is the shared runtime path.
    """

    name: ClassVar[str]

    def __init__(self, opts: BackendOpts):
        self.opts = opts

    @abstractmethod
    def build_command(self, inv: AgentInvocation) -> list[str]: ...

    @abstractmethod
    def parse_output(self, stdout: str, stderr: str, exit_code: int) -> AgentResult: ...

    def invoke(self, inv: AgentInvocation) -> AgentResult:
        cmd = self.build_command(inv)
        env = inv.env or LocalEnv()
        start = time.monotonic()
        try:
            proc = env.run_argv(cmd, cwd=inv.cwd, timeout=inv.timeout_s)
        except subprocess.TimeoutExpired as exc:
            return AgentResult(
                ok=False,
                output=_text(exc.stdout),
                session_id=None,
                exit_code=-1,
                duration_s=time.monotonic() - start,
                stderr_tail=_text(exc.stderr)[-2000:],
                error=f"agent timed out after {inv.timeout_s}s",
            )
        except FileNotFoundError:
            return AgentResult(
                ok=False,
                output="",
                session_id=None,
                exit_code=-1,
                duration_s=time.monotonic() - start,
                error=f"backend binary not found: {cmd[0]!r}",
            )
        result = self.parse_output(proc.stdout, proc.stderr, proc.returncode)
        result.duration_s = time.monotonic() - start
        return result


def _text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data
