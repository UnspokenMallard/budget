"""Quota-aware scheduling: should a heavy job wait for headroom?

A job opts in with `defer_on_low_quota = true` in its payload. Before it is
enqueued, `enqueue_due_jobs` asks `low_headroom()`. Deferred means the job's
`next_run` stays where it is, so the next tick asks again, and a run that was
deferred for hours fires once when headroom returns (missed slots are dropped,
as they always are).

Two signals, in order:
  1. A recorded limit window for Claude (`limits.blocked`). This is a real
     answer from the provider, so it always counts.
  2. A configured 5 h budget (`usage/status-config.json`,
     `claude_5h_budget_tokens`) with the transcript estimate at or above
     `[quota] threshold_pct` of it. The estimate is not the server's own
     percentage, so this only applies once someone has set a budget on purpose.

With neither, headroom is treated as available and the reason says so. Nothing
here guesses a cap: the usage files say the budget is null, and that stays null.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from . import store
from .store import log

USAGE_DIR = store.STATE / "usage"
STATUS_PATH = USAGE_DIR / "status.json"
CONFIG_PATH = USAGE_DIR / "status-config.json"
DEFAULT_THRESHOLD_PCT = 80


def _read(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def low_headroom(cfg: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """(is headroom low, why). Never raises: an unreadable signal means no deferral."""
    from . import limits

    try:
        blocked, until = limits.blocked(limits.CLAUDE, cfg)
    except Exception as exc:  # noqa: BLE001 - a broken limit read must not stall every job
        log.debug("quota: limit check failed: %s", exc)
        blocked, until = False, None
    if blocked:
        return True, f"Claude limit window open until {until}"

    cfg = cfg if cfg is not None else store.load_config()
    threshold = float((cfg.get("quota") or {}).get("threshold_pct", DEFAULT_THRESHOLD_PCT))
    budget = _read(CONFIG_PATH).get("claude_5h_budget_tokens")
    if not budget:
        return False, "no 5h budget configured; only a recorded limit window defers"
    used = (_read(STATUS_PATH).get("claude") or {}).get("five_hour_billable_tokens")
    if used is None:
        return False, "no usage estimate yet"
    pct = 100.0 * float(used) / float(budget)
    if pct >= threshold:
        return True, f"5h estimate {pct:.0f}% of budget (threshold {threshold:.0f}%)"
    return False, f"5h estimate {pct:.0f}% of budget"
