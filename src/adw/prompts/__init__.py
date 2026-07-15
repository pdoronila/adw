"""Prompt template loading and rendering."""

from __future__ import annotations

from pathlib import Path

_PROMPT_DIR = Path(__file__).parent


def render(name: str, **kwargs: str) -> str:
    template = (_PROMPT_DIR / f"{name}.md").read_text()
    return template.format(**kwargs)
