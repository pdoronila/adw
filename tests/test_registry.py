"""Tests for the global repo registry behind the multi-repo dashboard."""

from __future__ import annotations

import re
from pathlib import Path

from adw import registry


def test_register_and_list_roundtrip(tmp_path: Path) -> None:
    reg = tmp_path / "repos.json"
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()

    registry.register_repo(alpha, path=reg)
    registry.register_repo(beta, path=reg)
    assert registry.list_repos(reg) == [alpha.resolve(), beta.resolve()]


def test_register_repo_twice_stores_once(tmp_path: Path) -> None:
    reg = tmp_path / "repos.json"
    repo = tmp_path / "alpha"
    repo.mkdir()

    registry.register_repo(repo, path=reg)
    registry.register_repo(repo, path=reg)
    assert registry.list_repos(reg) == [repo.resolve()]


def test_list_repos_missing_file(tmp_path: Path) -> None:
    assert registry.list_repos(tmp_path / "nope.json") == []


def test_list_repos_corrupt_json(tmp_path: Path) -> None:
    reg = tmp_path / "repos.json"
    reg.write_text("{not json")
    assert registry.list_repos(reg) == []


def test_list_repos_drops_missing_directories(tmp_path: Path) -> None:
    reg = tmp_path / "repos.json"
    kept = tmp_path / "kept"
    gone = tmp_path / "gone"
    kept.mkdir()
    gone.mkdir()
    registry.register_repo(kept, path=reg)
    registry.register_repo(gone, path=reg)

    gone.rmdir()
    assert registry.list_repos(reg) == [kept.resolve()]


def test_repo_slugs_kebab_and_collisions(tmp_path: Path) -> None:
    fancy = tmp_path / "My_Cool Repo!"
    app_x = tmp_path / "x" / "app"
    app_y = tmp_path / "y" / "app"

    assert registry.repo_slugs([fancy]) == [("my-cool-repo", fancy)]
    assert registry.repo_slugs([app_x, app_y]) == [("app", app_x), ("app-2", app_y)]


def test_repo_slug_stable_and_unique(tmp_path: Path) -> None:
    app_x = tmp_path / "x" / "app"
    app_y = tmp_path / "y" / "app"
    app_x.mkdir(parents=True)
    app_y.mkdir(parents=True)

    slug_x = registry.repo_slug(app_x)
    assert slug_x != registry.repo_slug(app_y)
    assert registry.repo_slug(app_x) == slug_x
    assert slug_x.startswith("app-")
    assert re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{8}", slug_x)
    # Independent of URL-slug dedup: repo_slugs would give app_y "app-2",
    # but its storage slug is the same whether or not app_x is listed.
    assert registry.repo_slug(app_y) == registry.repo_slug(app_y)
