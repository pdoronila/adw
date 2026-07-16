from __future__ import annotations

import pytest

from adw.workflows.steps import _commit_subject


def test_commit_subject_short_task_passes_through() -> None:
    subject = _commit_subject("Add CSV export to reports", "feature")
    assert subject == "feat: Add CSV export to reports"


def test_commit_subject_truncates_long_first_line() -> None:
    long_line = "x" * 100
    subject = _commit_subject(long_line, "bug")
    assert subject == f"fix: {'x' * 72}"


def test_commit_subject_uses_first_line_only() -> None:
    task = "Fix the thing\n\nMore detail on the second paragraph."
    assert _commit_subject(task, "chore") == "chore: Fix the thing"


@pytest.mark.parametrize(
    "workflow,expected_type",
    [
        ("feature", "feat"),
        ("bug", "fix"),
        ("chore", "chore"),
        ("hotfix", "fix"),
        ("cve", "fix"),
        ("unknown-future-workflow", "chore"),
    ],
)
def test_commit_subject_type_mapping(workflow: str, expected_type: str) -> None:
    assert _commit_subject("Do the thing", workflow) == f"{expected_type}: Do the thing"
