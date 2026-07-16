"""Factory router: agent classification + keyword fallback."""

from __future__ import annotations

import json
from pathlib import Path

from adw.adapters.mock import MockAdapter, ScriptedTurn
from adw.config import AdwConfig
from adw.routing import keyword_route, route
from adw.workflows import WORKFLOWS

NAMES = list(WORKFLOWS)


def cfg() -> AdwConfig:
    return AdwConfig.model_validate({"agents": {"default": {"backend": "claude-code"}}})


def test_keyword_route() -> None:
    assert keyword_route("Production is down, checkout 500s", NAMES) == "hotfix"
    assert keyword_route("search returns the wrong results", NAMES) == "bug"
    assert keyword_route("CVE-2024-1: path traversal in loader", NAMES) == "cve"
    assert keyword_route("bump ruff and fix lint", NAMES) == "chore"
    assert keyword_route("add a CSV export screen", NAMES) == "feature"


def test_route_uses_agent_json(tmp_path: Path) -> None:
    payload = json.dumps(
        {"workflow": "bug", "task": "fix off-by-one in paginate()", "rationale": "existing defect"}
    )
    adapter = MockAdapter([ScriptedTurn(output=f"```json\n{payload}\n```")])
    result = route("pagination is off by one", cfg(), tmp_path, adapter=adapter)
    assert result.method == "agent"
    assert result.workflow == "bug"
    assert result.task == "fix off-by-one in paginate()"


def test_route_falls_back_on_bad_json(tmp_path: Path) -> None:
    adapter = MockAdapter([ScriptedTurn(output="I think this is a bug, definitely.")])
    result = route("the thing is broken and throws", cfg(), tmp_path, adapter=adapter)
    assert result.method == "fallback"
    assert result.workflow == "bug"  # matched by keyword
    assert result.task == "the thing is broken and throws"


def test_route_falls_back_on_invalid_workflow(tmp_path: Path) -> None:
    adapter = MockAdapter([ScriptedTurn(output='{"workflow": "refactor", "task": "x"}')])
    result = route("please add a dark mode toggle", cfg(), tmp_path, adapter=adapter)
    assert result.method == "fallback"
    assert result.workflow == "feature"


def test_route_falls_back_when_agent_errors(tmp_path: Path) -> None:
    adapter = MockAdapter([ScriptedTurn(ok=False, output="")])
    result = route("production outage in billing", cfg(), tmp_path, adapter=adapter)
    assert result.method == "fallback"
    assert result.workflow == "hotfix"
