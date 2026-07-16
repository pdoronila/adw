#!/usr/bin/env python3
"""Scaffold and run adw workflow examples.

Each examples/<name>/ directory is a seed repo plus an example.json describing
the workflow and task. This copies one into a fresh git repo and (optionally)
runs `adw run` against it, so you can exercise a workflow end to end without
building fixtures by hand.

    python examples/scaffold.py list
    python examples/scaffold.py setup cve          # seed only, print the command
    python examples/scaffold.py run   chore -y      # seed and run unattended

Destination defaults to /tmp/adw-examples/<name> (override with --dest or the
ADW_EXAMPLES_DIR env var).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXAMPLES_DIR.parent
DEFAULT_DEST_ROOT = Path(os.environ.get("ADW_EXAMPLES_DIR", "/tmp/adw-examples"))
GITIGNORE = "__pycache__/\n*.pyc\n.adw/\n"


def discover() -> dict[str, dict]:
    examples = {}
    for meta_path in sorted(EXAMPLES_DIR.glob("*/example.json")):
        examples[meta_path.parent.name] = json.loads(meta_path.read_text())
    return examples


def _git(cwd: Path, *args: str) -> None:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {proc.stderr.strip()}")


def materialize(name: str, dest: Path) -> dict:
    src = EXAMPLES_DIR / name
    if not (src / "example.json").is_file():
        avail = ", ".join(discover()) or "(none)"
        sys.exit(f"unknown example {name!r}; available: {avail}")
    meta = json.loads((src / "example.json").read_text())
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    (dest / "example.json").unlink()
    (dest / ".gitignore").write_text(GITIGNORE)
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "adw-examples@example.com")
    _git(dest, "config", "user.name", "adw examples")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "seed")
    return meta


def cmd_list(_args: argparse.Namespace) -> None:
    examples = discover()
    if not examples:
        print("no examples found")
        return
    for name, meta in examples.items():
        print(f"{name:<9} [{meta['workflow']:<7}] {meta['task'][:78]}")


def _dest_for(args: argparse.Namespace) -> Path:
    return (Path(args.dest) if args.dest else DEFAULT_DEST_ROOT / args.name).resolve()


def cmd_setup(args: argparse.Namespace) -> None:
    dest = _dest_for(args)
    meta = materialize(args.name, dest)
    print(f"seeded '{args.name}' at {dest}")
    tail = " -y" if args.yes else ""
    print("\nrun it with:\n")
    print(f"  uv run adw run {meta['workflow']} {json.dumps(meta['task'])} --repo {dest}{tail}\n")


def cmd_run(args: argparse.Namespace) -> None:
    dest = _dest_for(args)
    meta = materialize(args.name, dest)
    print(f"seeded '{args.name}' at {dest}\n")
    cmd = ["uv", "run", "adw", "run", meta["workflow"], meta["task"], "--repo", str(dest)]
    if args.yes:
        cmd.append("-y")
    # Run from the adw repo so `uv run adw` resolves this project; stream output.
    sys.exit(subprocess.run(cmd, cwd=REPO_ROOT).returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list available examples").set_defaults(func=cmd_list)

    p_setup = sub.add_parser("setup", help="seed a fresh repo and print the run command")
    p_setup.add_argument("name")
    p_setup.add_argument("--dest", help="where to create the repo")
    p_setup.add_argument("-y", "--yes", action="store_true", help="print an unattended command")
    p_setup.set_defaults(func=cmd_setup)

    p_run = sub.add_parser("run", help="seed a fresh repo and run the workflow")
    p_run.add_argument("name")
    p_run.add_argument("--dest", help="where to create the repo")
    p_run.add_argument("-y", "--yes", action="store_true", help="skip the engineer gates")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
