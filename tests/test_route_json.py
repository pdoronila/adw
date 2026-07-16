"""Tests for `adw route --json` output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adw import routing
from adw.cli import app
from adw.routing import RouteResult

runner = CliRunner()

EXPECTED_KEYS = {"workflow", "task", "rationale", "method"}


def test_route_json_prints_result_and_does_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routing,
        "route",
        lambda task, config, repo: RouteResult(
            "bug", task, "no agent classification; matched by keywords", "fallback"
        ),
    )

    result = runner.invoke(
        app, ["route", "the checkout page is broken", "--json", "--repo", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    assert isinstance(data, dict)
    assert set(data) == EXPECTED_KEYS
    assert data["workflow"] == "bug"
    assert data["task"] == "the checkout page is broken"
    assert data["method"] == "fallback"


def test_route_json_wins_over_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routing,
        "route",
        lambda task, config, repo: RouteResult(
            "bug", task, "no agent classification; matched by keywords", "fallback"
        ),
    )
    monkeypatch.setattr("adw.cli._execute", lambda *a, **k: pytest.fail("must not execute"))

    result = runner.invoke(
        app,
        ["route", "the checkout page is broken", "--json", "--run", "--repo", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    data = json.loads(result.output)
    assert set(data) == EXPECTED_KEYS
