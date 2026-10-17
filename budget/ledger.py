"""The spend ledger and the ceilings (moved from Jarvis's daemon/burn.py, 2026-10-17).

Meters what the machine's own automation spent, per Claude account (`scope`: personal, work):
- `provider_calls`: every model call by a daemon surface, the HUD or a local model. Call count
  is the runaway signal even where cost is unknown, so a call is recorded with or without a price
  (`cost_usd` NULL = unknown, never 0.0: a ledger must not understate spend).
- `job_calls`: one row per nightly LLM job run; read by the weekly and daily ceilings, never by
  the burn rate (one ordinary portfolio-advisor run costs more than the 15-minute limit).
- `usage_samples`: host usage snapshots (transcript estimate, includes Anze's own sessions), kept
  because nothing else keeps their history; reported, not alerted on.

Brain escalations are recorded here like every other call (brain keeps its own `brain_calls` in
jarvis.db only for its caps). Until 2026-10-17 the burn rate added `brain_calls` on top of
`provider_calls`, which already held the same rows, so every brain call counted twice.

Recording never raises into the caller: metering must not break the work it measures.
"""
from __future__ import annotations

import json
import time
from typing import Any, Mapping

from . import store
from .store import log

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, surface TEXT NOT NULL, model TEXT,
    ok INTEGER NOT NULL, duration_s REAL, cost_usd REAL, detail TEXT, scope TEXT NOT NULL DEFAULT 'personal');
CREATE INDEX IF NOT EXISTS idx_provider_calls_ts ON provider_calls(ts);
CREATE TABLE IF NOT EXISTS usage_samples (
    ts REAL NOT NULL PRIMARY KEY, five_hour_tokens INTEGER, seven_day_tokens INTEGER, est_cost_usd REAL,
    scope TEXT NOT NULL DEFAULT 'personal');
CREATE TABLE IF NOT EXISTS job_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, job TEXT NOT NULL, model TEXT,
    ok INTEGER NOT NULL, duration_s REAL, cost_usd REAL, detail TEXT, scope TEXT NOT NULL DEFAULT 'personal');
CREATE INDEX IF NOT EXISTS idx_job_calls_ts ON job_calls(ts);
"""

SCOPES = ("personal", "work")
# Kept equal to config.toml's value, so a missing key can never raise the ceiling.
JOB_WEEKLY_BUDGET_DEFAULT = 40.0
# Share of the weekly ceiling at which optional jobs stop starting (added 2026-10-09).
OPTIONAL_PAUSE_FRACTION = 0.8


def usage_dir():
    return store.STATE / "usage"


def ensure_schema() -> None:
    with store.connect() as conn:
        conn.executescript(_SCHEMA)


def cfg_burn(cfg: Mapping[str, Any] | None = None) -> dict:
    cfg = cfg if cfg is not None else store.load_config()
    section = dict(cfg.get("burn") or {})
    for k, v in (("enabled", True), ("window_s", 900), ("max_calls_per_window", 12), ("max_spend_usd_per_window", 1.50),
                 ("brake_enabled", True), ("brake_after_consecutive_windows", 3), ("brake_clears_after_s", 3600)):
        section.setdefault(k, v)
    return section


def record_job(job: str, ok: bool, duration_s: float, cost_usd: float | None = None, model: str = "",
               detail: str = "", scope: str = "personal") -> None:
    """Record one nightly-job run. Never raises into the caller."""
    try:
        ensure_schema()
        with store.connect() as conn:
            conn.execute("INSERT INTO job_calls(ts, job, model, ok, duration_s, cost_usd, detail, scope) "
                         "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                         (time.time(), job, model, 1 if ok else 0, duration_s, cost_usd, detail[:500], scope))
    except Exception as exc:  # noqa: BLE001
        log.debug("budget: could not record job %s: %s", job, exc)


def record_call(surface: str, ok: bool, duration_s: float, cost_usd: float | None = None, model: str = "",
                detail: str = "", scope: str = "personal") -> None:
    """Record one provider call. Never raises into the caller."""
    try:
        ensure_schema()
        with store.connect() as conn:
            conn.execute("INSERT INTO provider_calls(ts, surface, model, ok, duration_s, cost_usd, detail, scope) "
                         "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                         (time.time(), surface, model, 1 if ok else 0, duration_s, cost_usd, detail[:500], scope))
    except Exception as exc:  # noqa: BLE001
        log.debug("budget: could not record %s call: %s", surface, exc)


def _read(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def sample_usage() -> dict | None:
    """Snapshot the host usage counters (state/usage/status.json and usage.json are overwritten in
    place on every refresh, so this is their only history). One row per account."""
    ensure_schema()
    status, usage = _read(usage_dir() / "status.json"), _read(usage_dir() / "usage.json")
    by_account = (usage.get("claude") or {}).get("byAccount") or {}
    now = time.time()
    rows = []
    for scope, status_key, usage_cost in (("personal", "claude", by_account.get("personal") or {}),
                                          ("work", "claudeWork", by_account.get("work") or {})):
        claude = status.get(status_key) or {}
        five, seven, cost = claude.get("five_hour_billable_tokens"), claude.get("seven_day_billable_tokens"), \
            usage_cost.get("estimatedCostUsd")
        if five is None and seven is None and cost is None:
            continue
        # ts is the primary key: the work row is nudged by a microsecond
        rows.append({"ts": now + (0.000001 if scope == "work" else 0), "scope": scope,
                     "five_hour_tokens": five, "seven_day_tokens": seven, "est_cost_usd": cost})
    try:
        with store.connect() as conn:
            for r in rows:
                conn.execute("INSERT OR REPLACE INTO usage_samples(ts, five_hour_tokens, seven_day_tokens, est_cost_usd, scope) "
                             "VALUES(?, ?, ?, ?, ?)",
                             (r["ts"], r["five_hour_tokens"], r["seven_day_tokens"], r["est_cost_usd"], r["scope"]))
    except Exception as exc:  # noqa: BLE001
        log.debug("budget: could not record usage sample: %s", exc)
    return rows[0] if rows else None


def call_rate(window_s: int, scope: str | None = None) -> tuple[int, float]:
    """(calls, total known spend) recorded inside the window; `scope` None sums both accounts."""
    ensure_schema()
    cutoff = time.time() - window_s
    extra, args = ("", (cutoff,)) if scope is None else (" AND scope = ?", (cutoff, scope))
    with store.connect() as conn:
        calls, spend = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM provider_calls WHERE ts >= ?{extra}", args).fetchone()
    return int(calls), float(spend)


def job_spend(days: float = 7, scope: str | None = None) -> tuple[float, int]:
    """(known spend, runs) of the nightly LLM jobs in the trailing window."""
    ensure_schema()
    cutoff = time.time() - days * 86400
    extra, args = ("", (cutoff,)) if scope is None else (" AND scope = ?", (cutoff, scope))
    with store.connect() as conn:
        spend, runs = conn.execute(
            f"SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM job_calls WHERE ts >= ?{extra}", args).fetchone()
    return float(spend), int(runs)


def job_spend_by_job() -> list[dict]:
    """Per job over 30 days: scope(s), runs and known spend in 7 and 30 days, failed runs.
    For Jarvis's job-value review and the HUD's Job value panel."""
    ensure_schema()
    week = time.time() - 7 * 86400
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT job, GROUP_CONCAT(DISTINCT scope), SUM(ts >= ?), ROUND(SUM(CASE WHEN ts >= ? THEN cost_usd ELSE 0 END), 2), "
            "COUNT(*), ROUND(SUM(cost_usd), 2), SUM(ok = 0) FROM job_calls WHERE ts >= ? GROUP BY job ORDER BY 6 DESC",
            (week, week, time.time() - 30 * 86400)).fetchall()
    keys = ("job", "scope", "runs7", "usd7", "runs30", "usd30", "failed30")
    return [dict(zip(keys, r)) for r in rows]


def five_hour_tokens(scope: str = "personal") -> float | None:
    """Billable tokens in the trailing 5h window (state/usage/status.json, refreshed hourly).
    Only the personal account is measured; None when unreadable, which leaves the 5h pause off."""
    if scope != "personal":
        return None
    try:
        return float(_read(usage_dir() / "status.json")["claude"]["five_hour_billable_tokens"])
    except (KeyError, TypeError, ValueError):
        return None


def job_budget(cfg: Mapping[str, Any] | None = None, scope: str = "personal") -> dict:
    """One account's LLM-job ceilings and where its spend stands ([llm_jobs]):
    weekly ceiling (`over`), optional daily ceiling (`over_daily`, in `over`), and the optional pause
    at 80% of the weekly ceiling, the daily ceiling or the 5h token pause (`paused_for_optional`)."""
    cfg = cfg if cfg is not None else store.load_config()
    llm = cfg.get("llm_jobs") or {}
    key = "weekly_budget_usd" if scope == "personal" else f"weekly_budget_usd_{scope}"
    ceiling = float(llm.get(key, JOB_WEEKLY_BUDGET_DEFAULT))
    spent, runs = job_spend(7, scope)
    daily_ceiling = float(llm["daily_budget_usd"]) if "daily_budget_usd" in llm else None
    spent_day, _ = job_spend(1, scope)
    over_daily = daily_ceiling is not None and spent_day >= daily_ceiling
    five_h = five_hour_tokens(scope)
    five_h_pause = llm.get("five_hour_pause_tokens")
    over_five_h = five_h is not None and five_h_pause is not None and five_h >= float(five_h_pause)
    return {"five_hour_tokens": five_h, "five_hour_pause_tokens": five_h_pause, "over_five_hour": over_five_h,
            "spent": spent, "runs": runs, "ceiling": ceiling, "scope": scope, "over": spent >= ceiling or over_daily,
            "over_daily": over_daily, "spent_day": spent_day, "daily_ceiling": daily_ceiling,
            "paused_for_optional": spent >= ceiling * OPTIONAL_PAUSE_FRACTION or over_daily or over_five_h}


def burn_tag(scope: str) -> str:
    """Finding prefix. Personal keeps the original `BURN:`; each brake reads only its own."""
    return "BURN:" if scope == "personal" else f"BURN[{scope}]:"


def burn_findings(cfg: Mapping[str, Any] | None = None) -> list[tuple[str, str]]:
    """Over-limit spending per account in the burn window, each with a stable alert key.
    A key names the condition; only the message carries the numbers."""
    s = cfg_burn(cfg)
    if not s["enabled"]:
        return []
    window, max_calls, max_spend = int(s["window_s"]), int(s["max_calls_per_window"]), float(s["max_spend_usd_per_window"])
    out = []
    for scope in SCOPES:  # each account on its own: a burst on one says nothing about the other
        calls, spend = call_rate(window, scope)
        if calls > max_calls:
            out.append((f"burn:calls:{scope}", f"{burn_tag(scope)} the daemon made {calls} {scope}-account call(s) in "
                                                f"{window // 60} min, over the limit of {max_calls}"))
        if spend > max_spend:
            out.append((f"burn:spend:{scope}", f"{burn_tag(scope)} the daemon spent ${spend:.2f} on the {scope} account "
                                                f"in {window // 60} min, over the limit of ${max_spend:.2f}"))
    return out


def prune(retain_days: int = 30) -> int:
    ensure_schema()
    cutoff = time.time() - retain_days * 86400
    with store.connect() as conn:
        dropped = conn.execute("DELETE FROM provider_calls WHERE ts < ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM usage_samples WHERE ts < ?", (cutoff,))
        # longer: the weekly ceiling needs 7 days, and last quarter's job cost is asked after the fact
        conn.execute("DELETE FROM job_calls WHERE ts < ?", (time.time() - 90 * 86400,))
    return dropped


def report_lines(cfg: Mapping[str, Any] | None = None) -> list[str]:
    s = cfg_burn(cfg)
    window = int(s["window_s"])
    lines = []
    for scope in SCOPES:
        calls, spend = call_rate(window, scope)
        day_calls, day_spend = call_rate(86400, scope)
        b = job_budget(cfg, scope)
        lines += [f"  [{scope}] last {window // 60} min : {calls} call(s), ${spend:.2f} known spend "
                  f"(limits {s['max_calls_per_window']} / ${s['max_spend_usd_per_window']:.2f})",
                  f"  [{scope}] last 24h     : {day_calls} call(s), ${day_spend:.2f} known spend",
                  f"  [{scope}] LLM jobs 7d  : ${b['spent']:.2f} of ${b['ceiling']:.2f} in {b['runs']} run(s)"]
    return lines
