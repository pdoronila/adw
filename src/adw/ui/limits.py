"""Best-effort session-limit probes for the sidebar (claude-code + codex).

Account quota is host-global metadata, not run output, so it lives here in the
UI layer rather than in the backend adapters. Neither CLI exposes a supported
machine-readable source, so both probes read what exists today:

- claude-code: the OAuth usage endpoint, with the token Claude Code itself
  stores in `~/.claude/.credentials.json` (or the macOS Keychain). Undocumented,
  so parsing is defensive and every failure degrades to "unavailable".
- codex: the last `token_count` event's `rate_limits` snapshot in the newest
  rollout under `~/.codex/sessions/` — a pure offline file read, as-of the
  last codex turn.

A probe failure of any kind (missing CLI, logged out, API-key auth, malformed
data, network down) must render as a muted dash, never 500 a page. Set
`ADW_UI_NO_LIMITS=1` to skip the probes entirely.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_OAUTH_BETA_HEADER = "oauth-2025-04-20"
_CACHE_TTL_SECONDS = 60.0


class SessionLimit(BaseModel):
    """One normalized rate-limit window for one backend."""

    backend: str  # "claude" | "codex"
    label: str  # "5h", "weekly", or "<n>m" for unrecognized windows
    used_percent: float
    resets_at: datetime | None = None
    as_of: datetime | None = None  # snapshot age (codex); None means fresh


def _parse_iso(value: object) -> datetime | None:
    """ISO-8601 string -> aware datetime, or None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _codex_window(window: object, as_of: datetime | None) -> SessionLimit | None:
    """Map one codex `rate_limits` window dict to a SessionLimit, or None."""
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    minutes = window.get("window_minutes")
    if not isinstance(used, int | float) or not isinstance(minutes, int | float):
        return None
    if minutes == 10080:
        label = "weekly"
    elif minutes == 300:
        label = "5h"
    else:
        label = f"{int(minutes)}m"
    resets = window.get("resets_at")
    resets_at = datetime.fromtimestamp(resets, tz=UTC) if isinstance(resets, int | float) else None
    return SessionLimit(
        backend="codex",
        label=label,
        used_percent=float(used),
        resets_at=resets_at,
        as_of=as_of,
    )


def codex_limits() -> list[SessionLimit]:
    """Rate-limit snapshot from the newest codex rollout, or [] when absent."""
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    rollouts = sorted(
        (codex_home / "sessions").glob("*/*/*/rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )
    if not rollouts:
        return []
    newest = rollouts[-1]
    for line in reversed(newest.read_text().splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        rate_limits = payload.get("rate_limits")
        if not isinstance(rate_limits, dict):
            continue
        as_of = _parse_iso(record.get("timestamp")) or datetime.fromtimestamp(
            newest.stat().st_mtime, tz=UTC
        )
        windows = (_codex_window(rate_limits.get(key), as_of) for key in ("primary", "secondary"))
        return [w for w in windows if w is not None]
    return []


def _token_from_payload(raw: str) -> str | None:
    """`accessToken` out of a Claude Code credentials JSON blob, or None."""
    creds = json.loads(raw)
    token = creds.get("claudeAiOauth", {}).get("accessToken") if isinstance(creds, dict) else None
    return token if isinstance(token, str) and token else None


def _claude_token() -> str | None:
    """Claude Code's OAuth access token from disk or the macOS Keychain."""
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        return _token_from_payload((config_dir / ".credentials.json").read_text())
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if proc.returncode == 0:
                return _token_from_payload(proc.stdout)
        except Exception:
            pass
    return None


def claude_limits() -> list[SessionLimit]:
    """Subscription usage windows from the OAuth usage endpoint, or [].

    Never refreshes the token: racing Claude Code's own rotation can invalidate
    the user's CLI login, so an expired token just reads as unavailable.
    """
    token = _claude_token()
    if token is None:
        return []
    request = urllib.request.Request(
        _USAGE_URL,
        headers={"Authorization": f"Bearer {token}", "anthropic-beta": _OAUTH_BETA_HEADER},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.loads(response.read())
    if not isinstance(data, dict):
        return []
    out: list[SessionLimit] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "weekly")):
        window = data.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("utilization")
        if not isinstance(used, int | float):
            continue
        out.append(
            SessionLimit(
                backend="claude",
                label=label,
                used_percent=float(used),
                resets_at=_parse_iso(window.get("resets_at")),
            )
        )
    return out


_cache: tuple[float, list[SessionLimit]] | None = None
_cache_lock = threading.Lock()


def session_limits() -> list[SessionLimit]:
    """All backends' limits, TTL-cached; every probe failure degrades to []."""
    if os.environ.get("ADW_UI_NO_LIMITS"):
        return []
    global _cache
    with _cache_lock:
        now = time.monotonic()
        if _cache is not None and now - _cache[0] < _CACHE_TTL_SECONDS:
            return _cache[1]
        out: list[SessionLimit] = []
        for fetch in (claude_limits, codex_limits):
            try:
                out.extend(fetch())
            except Exception:
                pass  # a probe must never 500 a page
        _cache = (now, out)
        return out
