"""Global registry of repos served by the adw dashboard (~/.config/adw/repos.json)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

REGISTRY_PATH = Path("~/.config/adw/repos.json").expanduser()


def registry_path() -> Path:
    """Registry file location, overridable via ADW_REPOS_FILE (tests, odd setups)."""
    override = os.environ.get("ADW_REPOS_FILE")
    return Path(override).expanduser() if override else REGISTRY_PATH


def list_repos(path: Path | None = None) -> list[Path]:
    """Registered repos that still exist, deduped, in registration order.

    A missing or corrupt registry file reads as empty — the dashboard must
    never fail to start over a bad registry.
    """
    registry = path or registry_path()
    try:
        raw = json.loads(registry.read_text())
        entries = raw.get("repos", [])
    except Exception:
        return []
    repos: list[Path] = []
    for entry in entries if isinstance(entries, list) else []:
        repo = Path(entry).expanduser().resolve()
        if repo.is_dir() and repo not in repos:
            repos.append(repo)
    return repos


def register_repo(repo: Path, path: Path | None = None) -> None:
    """Add `repo` to the registry (no-op if already present)."""
    registry = path or registry_path()
    repo = repo.expanduser().resolve()
    repos = list_repos(registry)
    if repo in repos:
        return
    repos.append(repo)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"repos": [str(p) for p in repos]}, indent=2) + "\n")


def repo_slugs(repos: list[Path]) -> list[tuple[str, Path]]:
    """URL slug per repo from its basename; collisions deduped with -2, -3, ..."""
    seen: dict[str, int] = {}
    out: list[tuple[str, Path]] = []
    for repo in repos:
        slug = re.sub(r"[^a-z0-9]+", "-", repo.name.lower()).strip("-") or "repo"
        count = seen.get(slug, 0) + 1
        seen[slug] = count
        out.append((slug if count == 1 else f"{slug}-{count}", repo))
    return out
