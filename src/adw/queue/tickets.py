"""File-based ticket queue: directory location IS the state.

<repo>/.adw/tickets/{queue,in_progress,done,failed,archived}/ — transitions are
atomic os.rename calls, which double as the lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

STATES = ("queue", "in_progress", "done", "failed")
# archived/ is a sibling of the board states, not part of STATES: it stays off
# the board and out of `queue list`, but still counts as done for blocked_by.
ARCHIVED = "archived"
FIND_STATES = STATES + (ARCHIVED,)
DEFAULT_PRIORITY = 5


class TicketError(ValueError):
    pass


@dataclass
class Ticket:
    path: Path
    workflow: str
    title: str
    body: str
    priority: int = DEFAULT_PRIORITY
    repo: Path | None = None
    source_run: str | None = None  # run id that auto-filed this ticket; None for human-created
    blocked_by: list[str] = field(default_factory=list)  # ticket stems that must be in done/

    @property
    def task(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()

    @property
    def id(self) -> str:
        return self.path.stem


def tickets_root(repo: Path) -> Path:
    return repo / ".adw" / "tickets"


def ensure_dirs(repo: Path) -> Path:
    root = tickets_root(repo)
    for state in FIND_STATES:
        (root / state).mkdir(parents=True, exist_ok=True)
    return root


def parse_ticket(path: Path) -> Ticket:
    text = path.read_text()
    if not text.startswith("---"):
        raise TicketError(f"{path}: missing YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise TicketError(f"{path}: malformed frontmatter")
    meta: dict[str, Any] = yaml.safe_load(parts[1]) or {}
    if "title" not in meta:
        raise TicketError(f"{path}: frontmatter must include 'title'")
    raw_blocked = meta.get("blocked_by") or []
    if isinstance(raw_blocked, str):
        raw_blocked = [raw_blocked]
    return Ticket(
        path=path,
        workflow=str(meta.get("workflow", "feature")),
        title=str(meta["title"]),
        body=parts[2].strip(),
        priority=int(meta.get("priority", DEFAULT_PRIORITY)),
        repo=Path(meta["repo"]).expanduser() if meta.get("repo") else None,
        source_run=str(meta["source_run"]) if meta.get("source_run") else None,
        blocked_by=[str(b) for b in raw_blocked],
    )


def write_ticket(
    repo: Path,
    title: str,
    body: str,
    workflow: str = "feature",
    priority: int = DEFAULT_PRIORITY,
    target_repo: Path | None = None,
    source_run: str | None = None,
    blocked_by: list[str] | None = None,
) -> Path:
    ensure_dirs(repo)
    from adw.state.run_state import slugify

    meta: dict[str, Any] = {"workflow": workflow, "title": title, "priority": priority}
    if target_repo is not None:
        meta["repo"] = str(target_repo)
    if source_run is not None:
        meta["source_run"] = source_run
    if blocked_by:
        meta["blocked_by"] = list(blocked_by)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    queue_dir = tickets_root(repo) / "queue"
    stem = f"{stamp}-{slugify(title)}"
    path = queue_dir / f"{stem}.md"
    # Two failures in the same second slugify to the same stem; disambiguate.
    seq = 2
    while path.exists():
        path = queue_dir / f"{stem}-{seq}.md"
        seq += 1
    frontmatter = yaml.safe_dump(meta, sort_keys=False).strip()
    path.write_text(f"---\n{frontmatter}\n---\n\n{body.strip()}\n")
    return path


def list_tickets(repo: Path, state: str = "queue") -> list[Ticket]:
    directory = tickets_root(repo) / state
    if not directory.is_dir():
        return []
    # Capture the sort key while reading each file — a concurrent claim_next may
    # rename tickets away, so never stat again during the sort.
    scored: list[tuple[int, float, Ticket]] = []
    for path in directory.glob("*.md"):
        try:
            ticket = parse_ticket(path)
            mtime = path.stat().st_mtime
        except (TicketError, OSError):
            continue
        scored.append((ticket.priority, mtime, ticket))
    scored.sort(key=lambda s: (s[0], s[1]))
    return [ticket for _, _, ticket in scored]


def done_stems(repo: Path) -> set[str]:
    """Stems of every ticket in done/ or archived/ — the set that satisfies blocked_by entries.

    Archived tickets stay "done" for dependency purposes, so archiving a blocker
    never strands its dependents.
    """
    root = tickets_root(repo)
    return {p.stem for state in ("done", ARCHIVED) for p in (root / state).glob("*.md")}


def pending_blockers(ticket: Ticket, done: set[str]) -> list[str]:
    """Blockers not yet satisfied (a blocker is satisfied iff its stem is in done/)."""
    return [b for b in ticket.blocked_by if b not in done]


def _find_cycle(tickets: list[Ticket]) -> list[str] | None:
    """Find a blocked_by cycle among these tickets, as a list of stems, or None.

    Only edges between tickets in the list matter — a blocker outside the queue
    (done, failed, in-flight, or nonexistent) can never form a cycle.
    """
    ids = {t.id for t in tickets}
    graph = {t.id: [b for b in t.blocked_by if b in ids] for t in tickets}
    color = dict.fromkeys(graph, 0)  # 0 white, 1 grey (on path), 2 black
    for start in graph:
        if color[start]:
            continue
        color[start] = 1
        path = [start]
        stack = [(start, iter(graph[start]))]
        while stack:
            node, neighbors = stack[-1]
            for nxt in neighbors:
                if color[nxt] == 1:  # back-edge: cycle from nxt to the path tip
                    return path[path.index(nxt):]
                if color[nxt] == 0:
                    color[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(graph[nxt])))
                    break
            else:
                color[node] = 2
                path.pop()
                stack.pop()
    return None


def claim_next(repo: Path) -> Ticket | None:
    """Atomically move the highest-priority claimable ticket into in_progress and return it.

    Tickets whose blockers are not all in done/ are skipped (priority order among
    the rest is unchanged); the rename remains the sole lock. If nothing is
    claimable but blocked tickets remain, a dependency cycle among queued tickets
    raises TicketError; otherwise returns None (callers inspect queue/ to tell
    "empty" from "all blocked").
    """
    ensure_dirs(repo)
    done = done_stems(repo)
    queued = list_tickets(repo, "queue")
    blocked: list[Ticket] = []
    for ticket in queued:
        if pending_blockers(ticket, done):
            blocked.append(ticket)
            continue
        target = tickets_root(repo) / "in_progress" / ticket.path.name
        try:
            ticket.path.rename(target)
        except OSError:
            continue
        ticket.path = target
        return ticket
    if blocked:
        cycle = _find_cycle(queued)
        if cycle:
            raise TicketError(
                "dependency cycle among queued tickets: " + " -> ".join(cycle + cycle[:1])
            )
    return None


def claim_ticket(repo: Path, needle: str) -> Ticket:
    """Atomically move one named queued ticket into in_progress and return it.

    Refuses tickets with unfinished blockers (dependencies are semantic — edit
    the ticket to drop them). The rename remains the sole lock, so this is
    race-safe against concurrent claim_next callers.
    """
    ensure_dirs(repo)
    try:
        ticket = find_ticket(repo, needle, ("queue",))
    except TicketError as exc:
        if "no ticket matches" not in str(exc):
            raise
        try:
            elsewhere = find_ticket(repo, needle)
        except TicketError:
            raise exc from None
        raise TicketError(
            f"ticket {elsewhere.id!r} is in {elsewhere.path.parent.name}/, not queue/"
        ) from exc
    pending = pending_blockers(ticket, done_stems(repo))
    if pending:
        raise TicketError(
            f"ticket {ticket.id!r} is blocked by unfinished ticket(s): {', '.join(pending)}"
        )
    target = tickets_root(repo) / "in_progress" / ticket.path.name
    try:
        ticket.path.rename(target)
    except OSError as exc:
        raise TicketError(f"ticket {ticket.id!r} was claimed by another worker") from exc
    ticket.path = target
    return ticket


def finish(ticket: Ticket, repo: Path, outcome: str, detail: str, run_id: str) -> Path:
    """Move an in_progress ticket to done/ or failed/ and append the result."""
    state = "done" if outcome == "shipped" else "failed"
    target = tickets_root(repo) / state / ticket.path.name
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    result_section = (
        f"\n\n## Result\n- outcome: {outcome}\n- run_id: {run_id}\n"
        f"- detail: {detail}\n- at: {stamp}\n"
    )
    with ticket.path.open("a") as handle:
        handle.write(result_section)
    ticket.path.rename(target)
    ticket.path = target
    return target


def find_ticket(repo: Path, needle: str, states: tuple[str, ...] = STATES) -> Ticket:
    """Locate one ticket across `states` by exact stem, or unique substring of stem/title."""
    candidates = [t for state in states for t in list_tickets(repo, state)]
    for ticket in candidates:
        if ticket.path.stem == needle:
            return ticket
    matches = [
        t for t in candidates
        if needle in t.path.stem or needle in t.title
    ]
    if not matches:
        raise TicketError(f"no ticket matches {needle!r}")
    if len(matches) > 1:
        names = ", ".join(t.path.stem for t in matches)
        raise TicketError(f"ambiguous match for {needle!r}: {names}")
    return matches[0]


def find_failed(repo: Path, needle: str) -> Ticket:
    """Locate one failed ticket by exact stem, or unique substring of stem/title."""
    return find_ticket(repo, needle, ("failed",))


def remove(ticket: Ticket) -> None:
    """Delete a ticket file outright (not a state transition, so no rename)."""
    ticket.path.unlink()


def set_priority(ticket: Ticket, priority: int) -> None:
    """Rewrite the frontmatter priority in place, preserving all keys and the body."""
    text = ticket.path.read_text()
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise TicketError(f"{ticket.path}: malformed frontmatter")
    meta: dict[str, Any] = yaml.safe_load(parts[1]) or {}
    meta["priority"] = priority
    frontmatter = yaml.safe_dump(meta, sort_keys=False).strip()
    ticket.path.write_text(f"---\n{frontmatter}\n---{parts[2]}")
    ticket.priority = priority


def archive(repo: Path, ticket: Ticket) -> Path:
    """Move a done ticket into archived/, preserving its content (incl. '## Result')."""
    ensure_dirs(repo)
    target = tickets_root(repo) / ARCHIVED / ticket.path.name
    try:
        ticket.path.rename(target)
    except OSError as exc:
        raise TicketError(f"ticket {ticket.id!r} was moved by another worker") from exc
    ticket.path = target
    return target


def requeue(repo: Path, ticket: Ticket) -> Path:
    """Move a failed, done, or archived ticket back to queue/, stripping any '## Result' section."""
    text = ticket.path.read_text()
    clean_text, _, _ = text.partition("\n\n## Result\n")
    ticket.path.write_text(clean_text if clean_text.endswith("\n") else clean_text + "\n")
    target = tickets_root(repo) / "queue" / ticket.path.name
    ticket.path.rename(target)
    ticket.path = target
    return target
