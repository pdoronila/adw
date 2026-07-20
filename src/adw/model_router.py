"""Per-role model router: downshift on usage pressure, upshift on repeated failures.

Within-backend only — the router only ever moves along the backend's own ladder,
never across backends. Every probe failure degrades to "no usage data" (run at
the target tier); routing must never fail a run.
"""

from __future__ import annotations

from collections.abc import Callable

from adw.config import AdwConfig, RoleAgent
from adw.limits import SessionLimit, session_limits
from adw.state.run_state import RunState

# Config/adapter backend names vs SessionLimit.backend values (the limits probe
# emits "claude", not "claude-code"). opencode has no probe -> never in this map.
_LIMITS_BACKEND = {"claude-code": "claude", "codex": "codex"}


def pick_model(
    role: str,
    role_agent: RoleAgent,
    _workflow: str | None,
    state: RunState,
    config: AdwConfig,
    limits_fn: Callable[[], list[SessionLimit]] = session_limits,
) -> tuple[str | None, str]:
    """Choose the model for one agent turn. Returns (model, human-readable reason).

    A role's configured model is its target tier on the backend's ladder
    (best -> cheapest): live usage past downshift_warn/downshift_critical moves
    it toward cheaper rungs, repeated gate/validation failures move it toward
    smarter ones — and any usage-driven downshift suppresses upshift entirely.
    """
    router = config.model_router
    ladder = router.ladders.get(role_agent.backend, [])
    if not router.enabled or role_agent.model is None or role_agent.model not in ladder:
        return (role_agent.model, "unrouted")
    base = ladder.index(role_agent.model)
    last = len(ladder) - 1

    probe_backend = _LIMITS_BACKEND.get(role_agent.backend)
    try:
        windows = limits_fn()
    except Exception:
        windows = []  # mirrors session_limits' own degrade; covers injected fns too
    matched = [w for w in windows if w.backend == probe_backend] if probe_backend else []
    peak_window = max(matched, key=lambda w: w.used_percent) if matched else None
    down = 0
    if peak_window is not None:
        # used_percent is 0-100; the config thresholds are fractions.
        if peak_window.used_percent >= router.downshift_critical * 100:
            down = last - base  # jump to the cheapest rung
        elif peak_window.used_percent >= router.downshift_warn * 100:
            down = 1

    fails = state.role_failures.get(role, 0)
    up = fails // router.escalate_after

    # Downshift is a hard cap: any usage pressure suppresses upshift entirely.
    idx = base + down if down else base - up
    idx = min(max(idx, 0), last)

    if down and peak_window is not None:
        pct = f"{peak_window.used_percent:.0f}"
        reason = f"downshift: {probe_backend} {peak_window.label} at {pct}% -> {ladder[idx]}"
        if up:
            reason += f" (upshift suppressed: {fails} failures)"
    elif up and idx != base:
        reason = f"upshift: {role} failed gates {fails}x -> {ladder[idx]}"
    else:
        reason = f"hold: {ladder[base]} at target tier"
    return (ladder[idx], reason)
