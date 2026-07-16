"""Where a command runs: on the host, or inside an Apple container.

Adapters and gates only *build* commands; the environment decides where they
execute. Git orchestration always stays on the host — only the (untrusted)
agent and gate commands are routed into a sandbox.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Protocol

from adw.config import AdwConfig, IsolationConfig


class ExecutionEnvironment(Protocol):
    def run_argv(
        self, argv: list[str], *, cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]: ...

    def run_shell(
        self, command: str, *, cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]: ...


class LocalEnv:
    """Run on the host — identical to a plain subprocess call."""

    def run_argv(
        self, argv: list[str], *, cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)

    def run_shell(
        self, command: str, *, cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )


class ContainerEnv:
    """Run inside an Apple `container` (one lightweight VM per invocation).

    The working directory is bind-mounted at `workdir`; secrets are passed as
    env vars (`-e NAME` forwards the host value), so `claude -p` authenticates
    via ANTHROPIC_API_KEY with no interactive login.
    """

    def __init__(self, cfg: IsolationConfig):
        self.cfg = cfg

    def _wrap(self, argv: list[str], cwd: Path) -> list[str]:
        wd = self.cfg.workdir
        cmd = [self.cfg.binary, "run", "--rm", "-v", f"{cwd}:{wd}", "-w", wd]
        for secret in self.cfg.secrets:
            if os.environ.get(secret):  # forward only secrets set on the host
                cmd += ["-e", secret]
        cmd.append(self.cfg.image)
        return cmd + argv

    def run_argv(
        self, argv: list[str], *, cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._wrap(argv, cwd), capture_output=True, text=True, timeout=timeout
        )

    def run_shell(
        self, command: str, *, cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        return self.run_argv(["sh", "-lc", command], cwd=cwd, timeout=timeout)


def make_env(config: AdwConfig) -> ExecutionEnvironment:
    if config.isolation.type == "container":
        return ContainerEnv(config.isolation)
    return LocalEnv()
