from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from adw.cli import app
from adw.queue.tickets import (
    TicketError,
    claim_next,
    claim_ticket,
    find_failed,
    find_ticket,
    finish,
    list_tickets,
    parse_ticket,
    remove,
    requeue,
    set_priority,
    tickets_root,
    write_ticket,
)


def test_write_and_parse(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "Add login", "Use JWT.", workflow="feature", priority=2)
    ticket = parse_ticket(path)
    assert ticket.title == "Add login"
    assert ticket.workflow == "feature"
    assert ticket.priority == 2
    assert "Use JWT." in ticket.body
    assert "Add login" in ticket.task


def test_parse_rejects_missing_frontmatter(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("just text")
    with pytest.raises(TicketError):
        parse_ticket(bad)


def test_priority_ordering(tmp_path: Path) -> None:
    write_ticket(tmp_path, "later", "", priority=9)
    write_ticket(tmp_path, "urgent", "", priority=1)
    tickets = list_tickets(tmp_path, "queue")
    assert [t.title for t in tickets] == ["urgent", "later"]


def test_claim_and_finish(tmp_path: Path) -> None:
    write_ticket(tmp_path, "work item", "body", priority=1)
    ticket = claim_next(tmp_path)
    assert ticket is not None
    assert ticket.path.parent.name == "in_progress"
    assert claim_next(tmp_path) is None  # queue drained

    final = finish(ticket, tmp_path, "shipped", "commit abc123", "run-1")
    assert final.parent.name == "done"
    text = final.read_text()
    assert "## Result" in text and "run-1" in text


def test_failed_outcome_goes_to_failed(tmp_path: Path) -> None:
    write_ticket(tmp_path, "doomed", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    final = finish(ticket, tmp_path, "failed", "gates exhausted", "run-2")
    assert final.parent.name == "failed"


def test_ticket_with_target_repo(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "cross repo", "", target_repo=Path("/some/other/repo"))
    ticket = parse_ticket(path)
    assert ticket.repo == Path("/some/other/repo")


def test_source_run_round_trip(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "investigate", "", source_run="run-123")
    assert parse_ticket(path).source_run == "run-123"


def test_source_run_defaults_none(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "plain", "")
    assert parse_ticket(path).source_run is None


def test_same_title_same_second_distinct_paths(tmp_path: Path) -> None:
    first = write_ticket(tmp_path, "same title", "")
    second = write_ticket(tmp_path, "same title", "")
    assert first != second
    assert first.exists() and second.exists()


def test_requeue_cleans_and_moves_ticket(tmp_path: Path) -> None:
    write_ticket(tmp_path, "doomed", "do the thing")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "failed", "gates exhausted", "run-3")

    failed = list_tickets(tmp_path, "failed")
    assert len(failed) == 1
    target = requeue(tmp_path, failed[0])

    assert target.parent.name == "queue"
    text = target.read_text()
    assert "## Result" not in text
    reparsed = parse_ticket(target)
    assert reparsed.title == "doomed"
    assert any(t.title == "doomed" for t in list_tickets(tmp_path, "queue"))


def test_find_failed_by_stem(tmp_path: Path) -> None:
    write_ticket(tmp_path, "doomed", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "failed", "boom", "run-4")

    failed = list_tickets(tmp_path, "failed")[0]
    found = find_failed(tmp_path, failed.path.stem)
    assert found.path == failed.path


def test_find_failed_by_unique_substring(tmp_path: Path) -> None:
    write_ticket(tmp_path, "fix login bug", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "failed", "boom", "run-5")

    found = find_failed(tmp_path, "login")
    assert found.title == "fix login bug"


def test_find_failed_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(TicketError):
        find_failed(tmp_path, "nonexistent")


def test_find_failed_raises_when_ambiguous(tmp_path: Path) -> None:
    for title in ("fix login bug", "fix login page"):
        write_ticket(tmp_path, title, "")
        ticket = claim_next(tmp_path)
        assert ticket is not None
        finish(ticket, tmp_path, "failed", "boom", "run-x")

    with pytest.raises(TicketError):
        find_failed(tmp_path, "login")


def test_find_ticket_by_stem_across_states(tmp_path: Path) -> None:
    write_ticket(tmp_path, "shipped work", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "shipped", "done", "run-6")

    done = list_tickets(tmp_path, "done")[0]
    found = find_ticket(tmp_path, done.path.stem)
    assert found.path == done.path


def test_find_ticket_scopes_to_states(tmp_path: Path) -> None:
    write_ticket(tmp_path, "queued only", "")
    queued = list_tickets(tmp_path, "queue")[0]
    with pytest.raises(TicketError):
        find_ticket(tmp_path, queued.path.stem, ("failed",))


def test_find_ticket_ambiguous_across_states(tmp_path: Path) -> None:
    write_ticket(tmp_path, "fix login bug", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "failed", "boom", "run-7")

    write_ticket(tmp_path, "fix login page", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "shipped", "ok", "run-8")

    with pytest.raises(TicketError):
        find_ticket(tmp_path, "login")


def test_remove_deletes_file(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "delete me", "")
    ticket = parse_ticket(path)
    remove(ticket)
    assert not path.exists()


def test_set_priority_rewrites_frontmatter(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "bump me", "the body", workflow="bug", priority=5)
    ticket = parse_ticket(path)
    set_priority(ticket, 1)
    assert ticket.priority == 1

    reparsed = parse_ticket(path)
    assert reparsed.priority == 1
    assert reparsed.title == "bump me"
    assert reparsed.workflow == "bug"
    assert "the body" in reparsed.body


def test_set_priority_preserves_repo_key_and_result_section(tmp_path: Path) -> None:
    write_ticket(tmp_path, "cross repo", "", target_repo=Path("/some/other/repo"))
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "failed", "boom", "run-9")

    failed = list_tickets(tmp_path, "failed")[0]
    set_priority(failed, 1)

    reparsed = parse_ticket(failed.path)
    assert reparsed.priority == 1
    assert reparsed.repo == Path("/some/other/repo")
    assert "## Result" in failed.path.read_text()


def test_requeue_from_done(tmp_path: Path) -> None:
    write_ticket(tmp_path, "shipped work", "do the thing")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "shipped", "commit abc", "run-10")

    done = list_tickets(tmp_path, "done")[0]
    target = requeue(tmp_path, done)
    assert target.parent.name == "queue"
    assert "## Result" not in target.read_text()
    assert any(t.title == "shipped work" for t in list_tickets(tmp_path, "queue"))


def test_claim_ticket_moves_to_in_progress(tmp_path: Path) -> None:
    write_ticket(tmp_path, "fix login bug", "")
    ticket = claim_ticket(tmp_path, "login")
    assert ticket.path.parent.name == "in_progress"
    assert ticket.path.exists()
    assert list_tickets(tmp_path, "queue") == []


def test_claim_ticket_blocked_raises(tmp_path: Path) -> None:
    blocker = write_ticket(tmp_path, "blocker", "")
    path = write_ticket(tmp_path, "dependent", "", blocked_by=[blocker.stem])
    with pytest.raises(TicketError) as exc_info:
        claim_ticket(tmp_path, "dependent")
    assert blocker.stem in str(exc_info.value)
    assert path.exists()  # still queued


def test_claim_ticket_blocker_done_succeeds(tmp_path: Path) -> None:
    write_ticket(tmp_path, "blocker", "")
    done = claim_next(tmp_path)
    assert done is not None
    finish(done, tmp_path, "shipped", "ok", "run-1")
    write_ticket(tmp_path, "dependent", "", blocked_by=[done.path.stem])

    ticket = claim_ticket(tmp_path, "dependent")
    assert ticket.path.parent.name == "in_progress"


def test_claim_ticket_wrong_state_message(tmp_path: Path) -> None:
    write_ticket(tmp_path, "shipped work", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "shipped", "ok", "run-2")

    with pytest.raises(TicketError, match=r"is in done/, not queue/"):
        claim_ticket(tmp_path, "shipped work")


def test_claim_ticket_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(TicketError, match="no ticket matches"):
        claim_ticket(tmp_path, "nonexistent")


def test_claim_ticket_ambiguous_raises(tmp_path: Path) -> None:
    write_ticket(tmp_path, "fix login bug", "")
    write_ticket(tmp_path, "fix login page", "")
    with pytest.raises(TicketError, match="ambiguous"):
        claim_ticket(tmp_path, "login")


def test_claim_ticket_race_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_ticket(tmp_path, "raced", "")
    stale = parse_ticket(path)
    # Simulate a concurrent worker claiming the ticket after our lookup.
    monkeypatch.setattr("adw.queue.tickets.find_ticket", lambda *args, **kwargs: stale)
    path.rename(tickets_root(tmp_path) / "in_progress" / path.name)

    with pytest.raises(TicketError, match="claimed by another worker"):
        claim_ticket(tmp_path, "raced")


runner = CliRunner()


def test_cli_queue_retry_all(tmp_path: Path) -> None:
    for title in ("first", "second"):
        write_ticket(tmp_path, title, "")
        ticket = claim_next(tmp_path)
        assert ticket is not None
        finish(ticket, tmp_path, "failed", "boom", "run-y")

    result = runner.invoke(app, ["queue", "retry", "--all", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert list_tickets(tmp_path, "failed") == []
    assert len(list_tickets(tmp_path, "queue")) == 2


def test_cli_queue_retry_nonexistent_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["queue", "retry", "nonexistent", "--repo", str(tmp_path)])
    assert result.exit_code != 0


def test_cli_queue_retry_requires_exactly_one_arg(tmp_path: Path) -> None:
    result = runner.invoke(app, ["queue", "retry", "--repo", str(tmp_path)])
    assert result.exit_code == 2


def test_cli_ticket_rm_with_yes(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "delete me", "")
    result = runner.invoke(app, ["ticket", "rm", path.stem, "-y", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not path.exists()


def test_cli_ticket_rm_prompts(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "keep me", "")
    result = runner.invoke(
        app, ["ticket", "rm", path.stem, "--repo", str(tmp_path)], input="n\n"
    )
    assert result.exit_code != 0
    assert path.exists()


def test_cli_ticket_bump(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "bump me", "", priority=5)
    result = runner.invoke(
        app, ["ticket", "bump", path.stem, "--priority", "1", "--repo", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert parse_ticket(path).priority == 1


def test_cli_ticket_requeue_from_done(tmp_path: Path) -> None:
    write_ticket(tmp_path, "shipped work", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "shipped", "ok", "run-11")

    done = list_tickets(tmp_path, "done")[0]
    result = runner.invoke(app, ["ticket", "requeue", done.path.stem, "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert list_tickets(tmp_path, "done") == []
    assert any(t.title == "shipped work" for t in list_tickets(tmp_path, "queue"))


def test_cli_ticket_edit_invokes_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_ticket(tmp_path, "edit me", "")
    captured: list[list[str]] = []

    def fake(argv: list[str]) -> None:
        captured.append(argv)

    monkeypatch.setattr("adw.cli.subprocess.run", fake)
    monkeypatch.setenv("EDITOR", "myeditor")
    result = runner.invoke(app, ["ticket", "edit", path.stem, "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert captured == [["myeditor", str(path)]]


def test_cli_ticket_rm_ambiguous_exits_nonzero(tmp_path: Path) -> None:
    write_ticket(tmp_path, "fix login bug", "")
    write_ticket(tmp_path, "fix login page", "")
    result = runner.invoke(app, ["ticket", "rm", "login", "-y", "--repo", str(tmp_path)])
    assert result.exit_code != 0


def _add_blocker(path: Path, stem: str) -> None:
    """Rewrite a ticket's frontmatter in place to add a blocked_by entry."""
    parts = path.read_text().split("---", 2)
    meta = yaml.safe_load(parts[1]) or {}
    meta.setdefault("blocked_by", []).append(stem)
    frontmatter = yaml.safe_dump(meta, sort_keys=False).strip()
    path.write_text(f"---\n{frontmatter}\n---{parts[2]}")


def test_blocked_by_round_trip(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "child", "", blocked_by=["a", "b"])
    assert parse_ticket(path).blocked_by == ["a", "b"]
    plain = write_ticket(tmp_path, "plain", "")
    assert parse_ticket(plain).blocked_by == []


def test_claim_ordering_with_dependency_chain(tmp_path: Path) -> None:
    a = write_ticket(tmp_path, "task a", "", priority=5)
    b = write_ticket(tmp_path, "task b", "", priority=1, blocked_by=[a.stem])
    write_ticket(tmp_path, "task c", "", priority=1, blocked_by=[b.stem])

    first = claim_next(tmp_path)
    assert first is not None and first.title == "task a"  # not higher-priority blocked b
    finish(first, tmp_path, "shipped", "ok", "run-a")

    second = claim_next(tmp_path)
    assert second is not None and second.title == "task b"
    assert claim_next(tmp_path) is None  # c blocked by in-progress (not done) b
    finish(second, tmp_path, "shipped", "ok", "run-b")

    third = claim_next(tmp_path)
    assert third is not None and third.title == "task c"


def test_blocked_by_failed_blocker_stays_blocked(tmp_path: Path) -> None:
    a = write_ticket(tmp_path, "doomed", "")
    write_ticket(tmp_path, "dependent", "", blocked_by=[a.stem])
    ticket = claim_next(tmp_path)
    assert ticket is not None and ticket.title == "doomed"
    finish(ticket, tmp_path, "failed", "boom", "run-f")

    assert claim_next(tmp_path) is None
    assert [t.title for t in list_tickets(tmp_path, "queue")] == ["dependent"]


def test_unknown_blocker_never_claims(tmp_path: Path) -> None:
    write_ticket(tmp_path, "stuck", "", blocked_by=["no-such-stem"])
    assert claim_next(tmp_path) is None


def test_cycle_detection_raises_at_claim(tmp_path: Path) -> None:
    a = write_ticket(tmp_path, "cycle a", "")
    b = write_ticket(tmp_path, "cycle b", "", blocked_by=[a.stem])
    _add_blocker(a, b.stem)

    with pytest.raises(TicketError) as exc_info:
        claim_next(tmp_path)
    message = str(exc_info.value)
    assert "cycle" in message
    assert a.stem in message and b.stem in message


def test_cycle_ignored_while_claimable_ticket_exists(tmp_path: Path) -> None:
    a = write_ticket(tmp_path, "cycle a", "")
    b = write_ticket(tmp_path, "cycle b", "", blocked_by=[a.stem])
    _add_blocker(a, b.stem)
    write_ticket(tmp_path, "free c", "")

    ticket = claim_next(tmp_path)
    assert ticket is not None and ticket.title == "free c"
    with pytest.raises(TicketError):
        claim_next(tmp_path)


def test_set_priority_preserves_blocked_by(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "child", "", blocked_by=["a", "b"])
    set_priority(parse_ticket(path), 1)
    reparsed = parse_ticket(path)
    assert reparsed.priority == 1
    assert reparsed.blocked_by == ["a", "b"]


def test_cli_ticket_new_blocked_by(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["ticket", "new", "child", "--blocked-by", "x", "--blocked-by", "y",
         "--repo", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "does not match any existing ticket" in result.output
    tickets = list_tickets(tmp_path, "queue")
    assert len(tickets) == 1
    assert tickets[0].blocked_by == ["x", "y"]


def test_cli_queue_list_marks_blocked(tmp_path: Path) -> None:
    dep = write_ticket(tmp_path, "dep done", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "shipped", "ok", "run-d")

    write_ticket(tmp_path, "waiting", "", blocked_by=[dep.stem, "missing-stem"])
    write_ticket(tmp_path, "free", "")

    result = runner.invoke(app, ["queue", "list", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "[blocked by: missing-stem]" in result.output  # satisfied blocker not listed
    assert result.output.count("blocked by:") == 1  # unblocked ticket has no marker


def test_cli_queue_process_all_blocked_message(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "stuck", "", blocked_by=["no-such-stem"])
    result = runner.invoke(app, ["queue", "process", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "blocked" in result.output
    assert "no-such-stem" in result.output
    assert "queue is empty" not in result.output
    assert path.exists()  # still queued


def test_cli_queue_process_specific_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_ticket(tmp_path, "skip me", "", priority=1)
    path = write_ticket(tmp_path, "pick me", "", priority=9)
    processed = []
    monkeypatch.setattr(
        "adw.cli._process_ticket",
        lambda ticket, repo, auto_approve_plan, yes: processed.append(ticket),
    )

    result = runner.invoke(app, ["queue", "process", path.stem, "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert [t.title for t in processed] == ["pick me"]  # not the higher-priority one
    assert processed[0].path.parent.name == "in_progress"


def test_cli_queue_process_ticket_excludes_all(tmp_path: Path) -> None:
    result = runner.invoke(app, ["queue", "process", "x", "--all", "--repo", str(tmp_path)])
    assert result.exit_code == 2
    result = runner.invoke(
        app, ["queue", "process", "x", "--parallel", "2", "-y", "--repo", str(tmp_path)]
    )
    assert result.exit_code == 2


def test_cli_queue_process_blocked_ticket_errors(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "stuck", "", blocked_by=["no-such-stem"])
    result = runner.invoke(app, ["queue", "process", path.stem, "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "no-such-stem" in result.output
    assert path.exists()  # still queued
