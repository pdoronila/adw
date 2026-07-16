"""Tests for `adw workflows --json` output."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from adw.cli import app
from adw.workflows import WORKFLOWS

runner = CliRunner()


def test_workflows_json_lists_registry() -> None:
    result = runner.invoke(app, ["workflows", "--json"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    assert isinstance(data, list)
    assert [entry["name"] for entry in data] == sorted(WORKFLOWS)
    for entry in data:
        assert set(entry) == {"name", "description"}
        assert entry["description"] == WORKFLOWS[entry["name"]].description


def test_workflows_text_listing_unchanged() -> None:
    result = runner.invoke(app, ["workflows"])
    assert result.exit_code == 0, result.output
    for name in WORKFLOWS:
        assert name in result.output
