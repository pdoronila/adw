"""Tests for `adw queue list --json` output."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from adw.cli import app
from adw.queue.tickets import STATES, write_ticket

runner = CliRunner()


def test_queue_list_json(tmp_path: Path) -> None:
    write_ticket(tmp_path, "First task", "body", workflow="feature", priority=1)
    write_ticket(tmp_path, "Second task", "body", workflow="fix", priority=3)

    result = runner.invoke(app, ["queue", "list", "--json", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    assert set(data) == set(STATES)
    assert [t["title"] for t in data["queue"]] == ["First task", "Second task"]
    for ticket in data["queue"]:
        assert set(ticket) == {"workflow", "title", "priority"}
    assert data["queue"][0] == {"workflow": "feature", "title": "First task", "priority": 1}
    assert data["in_progress"] == []


def test_queue_list_json_empty(tmp_path: Path) -> None:
    result = runner.invoke(app, ["queue", "list", "--json", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {state: [] for state in STATES}
