"""Generate a starter adw.yaml by inspecting a target repository."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path
from typing import Any

import yaml

from adw.config import AdwConfig, GateConfig, StrictModel

_GATE_ORDER = ("lint", "typecheck", "test")


class ProjectProfile(StrictModel):
    ecosystem: str
    gates: dict[str, GateConfig]
    notes: list[str]


def _gate(command: str, timeout: int) -> GateConfig:
    return GateConfig(command=command, timeout=timeout)


def _detect_python(repo: Path) -> ProjectProfile:
    notes: list[str] = []
    data: dict[str, Any] = {}
    try:
        data = tomllib.loads((repo / "pyproject.toml").read_text())
    except tomllib.TOMLDecodeError:
        notes.append("pyproject.toml could not be parsed; gates guessed from files only")

    tool = data.get("tool", {}) if isinstance(data, dict) else {}
    project = data.get("project", {}) if isinstance(data, dict) else {}

    prefix = "uv run " if (repo / "uv.lock").exists() or "uv" in tool else ""

    deps: list[str] = []
    if isinstance(project.get("dependencies"), list):
        deps.extend(project["dependencies"])
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for group in optional.values():
            if isinstance(group, list):
                deps.extend(group)
    groups = data.get("dependency-groups", {}) if isinstance(data, dict) else {}
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, list):
                deps.extend(item for item in group if isinstance(item, str))

    def has_dep(name: str) -> bool:
        return any(isinstance(dep, str) and dep.startswith(name) for dep in deps)

    gates: dict[str, GateConfig] = {}
    if "ruff" in tool or has_dep("ruff"):
        gates["lint"] = _gate(f"{prefix}ruff check .", 120)
    if "mypy" in tool or has_dep("mypy"):
        gates["typecheck"] = _gate(f"{prefix}mypy", 300)
    pytest_tool = "pytest" in tool
    if pytest_tool or has_dep("pytest") or (repo / "tests").is_dir():
        gates["test"] = _gate(f"{prefix}pytest -x -q", 900)

    if not gates:
        gates["test"] = _gate(f"{prefix}pytest -x -q", 900)
        notes.append("no test tooling detected; assumed pytest — edit adw.yaml if wrong")

    return ProjectProfile(ecosystem="python", gates=gates, notes=notes)


def _detect_node(repo: Path) -> ProjectProfile:
    notes: list[str] = []
    scripts: dict[str, Any] = {}
    try:
        data = json.loads((repo / "package.json").read_text())
        if isinstance(data, dict) and isinstance(data.get("scripts"), dict):
            scripts = data["scripts"]
    except json.JSONDecodeError:
        notes.append("package.json could not be parsed; gates guessed from files only")

    if (repo / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    elif (repo / "yarn.lock").exists():
        pm = "yarn"
    elif (repo / "bun.lockb").exists() or (repo / "bun.lock").exists():
        pm = "bun"
    else:
        pm = "npm"

    gates: dict[str, GateConfig] = {}
    if "lint" in scripts:
        gates["lint"] = _gate(f"{pm} run lint", 120)
    if "typecheck" in scripts:
        gates["typecheck"] = _gate(f"{pm} run typecheck", 300)
    elif (repo / "tsconfig.json").exists():
        gates["typecheck"] = _gate("npx tsc --noEmit", 300)
    if "test" in scripts:
        gates["test"] = _gate(f"{pm} test", 900)

    return ProjectProfile(ecosystem="node", gates=gates, notes=notes)


def _detect_rust(repo: Path) -> ProjectProfile:
    gates = {
        "lint": _gate("cargo clippy -- -D warnings", 120),
        "test": _gate("cargo test", 900),
    }
    return ProjectProfile(ecosystem="rust", gates=gates, notes=[])


def _detect_go(repo: Path) -> ProjectProfile:
    gates = {
        "lint": _gate("go vet ./...", 120),
        "test": _gate("go test ./...", 900),
    }
    return ProjectProfile(ecosystem="go", gates=gates, notes=[])


def _has_swiftlint(repo: Path) -> bool:
    return (repo / ".swiftlint.yml").exists() or (repo / ".swiftlint.yaml").exists()


def _detect_swift(repo: Path) -> ProjectProfile:
    gates = {"test": _gate("swift test", 900)}
    if _has_swiftlint(repo):
        gates["lint"] = _gate("swiftlint", 120)
    return ProjectProfile(ecosystem="swift", gates=gates, notes=[])


def _detect_ios(repo: Path) -> ProjectProfile:
    workspace = next(iter(sorted(repo.glob("*.xcworkspace"))), None)
    project = next(iter(sorted(repo.glob("*.xcodeproj"))), None)
    destination = "platform=iOS Simulator,name=iPhone 16"
    if workspace is not None:
        scheme = workspace.stem
        command = (
            f"xcodebuild test -workspace {workspace.name} -scheme {scheme} "
            f"-destination '{destination}' -quiet"
        )
    else:
        assert project is not None  # detect_project only calls us when a container exists
        scheme = project.stem
        command = (
            f"xcodebuild test -project {project.name} -scheme {scheme} "
            f"-destination '{destination}' -quiet"
        )

    gates = {"test": _gate(command, 1800)}
    if _has_swiftlint(repo):
        gates["lint"] = _gate("swiftlint", 120)
    notes = [
        "scheme and simulator destination were guessed — "
        "verify with `xcodebuild -list` and edit adw.yaml if wrong"
    ]
    return ProjectProfile(ecosystem="ios", gates=gates, notes=notes)


def _detect_elixir(repo: Path) -> ProjectProfile:
    notes: list[str] = []
    try:
        source = (repo / "mix.exs").read_text()
    except OSError:
        source = ""
        notes.append("mix.exs could not be read; gates guessed from files only")

    gates: dict[str, GateConfig] = {}
    if ":credo" in source:
        gates["lint"] = _gate("mix credo", 120)
    else:
        gates["lint"] = _gate("mix format --check-formatted", 120)
    if ":dialyxir" in source:
        gates["typecheck"] = _gate("mix dialyzer", 600)
    gates["test"] = _gate("mix test", 900)

    return ProjectProfile(ecosystem="elixir", gates=gates, notes=notes)


def detect_project(repo: Path) -> ProjectProfile:
    """Detect ecosystem and gates from file markers, first match wins."""
    if (repo / "pyproject.toml").exists():
        return _detect_python(repo)
    if (repo / "package.json").exists():
        return _detect_node(repo)
    if (repo / "Cargo.toml").exists():
        return _detect_rust(repo)
    if (repo / "go.mod").exists():
        return _detect_go(repo)
    if any(repo.glob("*.xcworkspace")) or any(repo.glob("*.xcodeproj")):
        return _detect_ios(repo)
    if (repo / "Package.swift").exists():
        return _detect_swift(repo)
    if (repo / "mix.exs").exists():
        return _detect_elixir(repo)
    return ProjectProfile(
        ecosystem="unknown",
        gates={},
        notes=["could not detect project type; add gates to adw.yaml by hand"],
    )


def detect_backend(config: AdwConfig) -> str:
    """Return the first installed agent backend, defaulting to claude-code."""
    for name in ("claude-code", "codex", "opencode"):
        if shutil.which(config.backends.for_backend(name).binary):
            return name
    return "claude-code"


def render_config(profile: ProjectProfile, backend: str) -> str:
    """Render a commented adw.yaml as a template string."""
    ordered = [name for name in _GATE_ORDER if name in profile.gates]
    lines = [
        f"# adw.yaml — generated by `adw init` (ecosystem: {profile.ecosystem}). Edit freely.",
        "version: 1",
        "",
    ]
    if ordered:
        lines.append("gates:")
        width = max(len(name) for name in ordered)
        for name in ordered:
            gate = profile.gates[name]
            command = json.dumps(gate.command)
            label = f"{name}:".ljust(width + 1)
            lines.append(f"  {label} {{command: {command}, timeout: {gate.timeout}}}")
    else:
        lines.append(
            "# no gates detected — add at least a test gate, "
            'e.g. {command: "pytest -x -q", timeout: 900}'
        )
        lines.append("gates: {}")
    lines += [
        "",
        "agents:",
        f"  default: {{backend: {backend}}}",
        "",
        "workflow:",
        "  max_fix_iterations: 3",
    ]
    if ordered:
        lines.append(f"  gate_order: [{', '.join(ordered)}]")
    lines += [
        "",
        "ship:",
        '  branch_prefix: "adw/"',
        "  create_pr: false",
        "",
        "# notify:                  # ping when a run pauses at a gate or fails",
        "#   macos: true            # macOS notification via osascript",
        '#   webhook: "https://..." # POST {run_id, status, workflow, task, repo}',
        "",
    ]
    return "\n".join(lines)


def validate_rendered(text: str) -> AdwConfig:
    """Round-trip the rendered YAML through the schema before it is written."""
    return AdwConfig.model_validate(yaml.safe_load(text) or {})
