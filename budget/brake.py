"""The burn brake's decision and state (moved from Jarvis's daemon/burn.py, 2026-10-17).

Deliberately narrow, as built in Jarvis's Phase 6:
- Only sustained BURN engages it (over the spend or call limit for `brake_after_consecutive_windows`
  consecutive windows, 3 x 15 min), never stuck events or repeated failures.
- It stops new model-spending work (Jarvis's queue dispatch and brain escalations); it never
  kills running work, and scheduled health-check jobs keep running.
- It self-clears after `brake_clears_after_s`: a brake that needs a human converts a spend
  incident into an outage. An unreadable brake file means no brake, for the same reason.

Budget decides and records; it never alerts. `consider()` returns whether this call engaged the
brake, and Jarvis's burn-check raises the alert.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import ledger, store
from .store import log

_STREAK = "burn_consecutive_over_limit"


def path(scope: str) -> Path:
    """One brake per Claude account; personal keeps the original name."""
    return store.STATE / ("brake.json" if scope == "personal" else f"brake-{scope}.json")


def _streak_key(scope: str) -> str:
    return _STREAK if scope == "personal" else f"{_STREAK}:{scope}"


def engaged(cfg: Mapping[str, Any] | None = None, scope: str = "personal") -> tuple[bool, dict | None]:
    """Is the brake on? Self-clears once its window has passed."""
    if not ledger.cfg_burn(cfg)["brake_enabled"]:
        return False, None
    try:
        state = json.loads(path(scope).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    if time.time() >= float(state.get("clears_at") or 0):
        release("its window expired", scope)
        return False, None
    return True, state


def engage(reason: str, evidence: list[str], cfg: Mapping[str, Any] | None = None, scope: str = "personal") -> dict:
    s = ledger.cfg_burn(cfg)
    now = time.time()
    clears = now + float(s["brake_clears_after_s"])
    human = lambda t: datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")  # noqa: E731
    state = {"engaged_at": human(now), "reason": reason, "evidence": evidence[:10], "clears_at": clears,
             "clears_at_human": human(clears), "scope": scope,
             "blocks": [f"queue-dispatch and brain escalations on the {scope} account"],
             "does_not_block": "scheduled script jobs, so health checks keep reporting",
             "note": "Delete this file to resume immediately."}
    try:
        p = path(scope)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("budget: could not write the brake file: %s", exc)
    log.warning("budget: brake engaged - %s", reason)
    return state


def release(why: str = "released", scope: str = "personal") -> None:
    try:
        p = path(scope)
        existed = p.exists()
        p.unlink(missing_ok=True)
        if existed:
            log.info("budget: brake released (%s)", why)
    except OSError as exc:
        log.debug("budget: could not clear the brake file: %s", exc)
    store.set_meta(_streak_key(scope), "0")


def consider(findings: list[str], cfg: Mapping[str, Any] | None = None,
             scope: str = "personal") -> tuple[str | None, dict | None]:
    """Count consecutive over-limit windows; engage once the run is long enough.
    Returns (note, state): `state` is set only when THIS call engaged the brake, so the caller
    alerts exactly once. One spike is not a runaway; the counter resets on a window under the limit."""
    s = ledger.cfg_burn(cfg)
    if not s["brake_enabled"]:
        return None, None
    burning = [f for f in findings if f.startswith(ledger.burn_tag(scope))]
    key = _streak_key(scope)
    if not burning:
        if int(store.get_meta(key) or 0):
            store.set_meta(key, "0")
        return None, None
    streak = int(store.get_meta(key) or 0) + 1
    store.set_meta(key, str(streak))
    need = int(s["brake_after_consecutive_windows"])
    if streak < need:
        return f"over the burn limit for {streak}/{need} consecutive window(s)", None
    if engaged(cfg, scope)[0]:
        return "already braked", None
    state = engage(f"the daemon has been over its {scope}-account burn limit for {streak} consecutive "
                   f"{int(s['window_s']) // 60} min window(s)", burning, cfg, scope)
    return "brake engaged", state
