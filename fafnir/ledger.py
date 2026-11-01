"""The spend ledger and the ceilings (moved from Jarvis's daemon/burn.py, 2026-10-17).

Meters what the machine's own automation spent, per Claude account (`scope`: personal, work):
- `provider_calls`: every model call by a daemon surface or the HUD. Call count
  is the runaway signal even where cost is unknown, so a call is recorded with or without a price
  (`cost_usd` NULL = unknown, never 0.0: a ledger must not understate spend).
- `job_calls`: one row per nightly LLM job run; read by the weekly and daily ceilings, never by
  the burn rate (one ordinary portfolio-advisor run costs more than the 15-minute limit).
- `usage_samples`: host usage snapshots (transcript estimate, includes Anze's own sessions), kept
  because nothing else keeps their history; reported, not alerted on.

Brain escalations are recorded here like every other call (brain keeps its own `brain_calls` in
jarvis.db only for its caps). Until 2026-10-17 the burn rate added `brain_calls` on top of
`provider_calls`, which already held the same rows, so every brain call counted twice.

Recording never raises into the caller: metering must not break the work it measures. The rows are
Mimir telemetry (append-only, kept 90 days): a write goes through Mimir's outbox, so it survives
Mimir being down. A rejection is its own row (`fafnir.job_reject`), joined to its job call when read.
Reading raises `mimirrecords.MimirDown` when Mimir does not answer: a ceiling must not be guessed.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any, Mapping

from . import store
from .store import log, mr

SOURCE = store.SOURCE
CALLS, JOBS, SAMPLES, REJECTS = "fafnir.provider_call", "fafnir.job_call", "fafnir.usage_sample", "fafnir.job_reject"

SCOPES = ("personal", "work")
# Kept equal to config.toml's value, so a missing key can never raise the ceiling.
JOB_WEEKLY_BUDGET_DEFAULT = 60.0
# Share of the weekly ceiling at which optional jobs stop starting (added 2026-10-09).
OPTIONAL_PAUSE_FRACTION = 1.0  # raised from 0.8 by Anze 2026-10-20
# Mimir returns at most this many rows per read. One read kept the oldest page and dropped the rest.
_PAGE = 5000
# status.json is refreshed hourly; older than this, the 5h token count is unknown (fail open).
STATUS_MAX_AGE_S = 3 * 3600


def usage_dir():
    return store.STATE / "usage"


def _rows(stream: str, since: float, scope: str | None = None) -> list[dict]:
    """The data of the stream's rows since `since`, each with `ts` (a call's time). Raises MimirDown.

    Pages of `_PAGE`. A full page is read again from its last `at` (inclusive) and ids already
    returned are skipped, so a tie on `at` is kept once and a window longer than one page is not
    cut off after the oldest rows. Stops when a page is short or adds no new id."""
    where = None if scope is None else "scope:" + json.dumps(scope)
    out: list[dict] = []
    seen: set = set()
    while True:
        page = mr.query(stream=stream, since=since, where=where, limit=_PAGE, source=SOURCE)
        fresh = [r for r in page if r["id"] not in seen]
        seen.update(r["id"] for r in page)
        out.extend({**r["data"], "ts": r["at"]} for r in fresh)
        if len(page) < _PAGE or not fresh:
            return out
        since = page[-1]["at"]


def cfg_burn(cfg: Mapping[str, Any] | None = None) -> dict:
    cfg = cfg if cfg is not None else store.load_config()
    section = dict(cfg.get("burn") or {})
    for k, v in (("enabled", True), ("window_s", 900), ("max_calls_per_window", 12), ("max_spend_usd_per_window", 1.50),
                 ("brake_enabled", True), ("brake_after_consecutive_windows", 3), ("brake_clears_after_s", 3600),
                 ("count_exclude_surfaces", ["hud-"])):
        section.setdefault(k, v)
    return section


def record_job(job: str, ok: bool, duration_s: float, cost_usd: float | None = None, model: str = "",
               detail: str = "", scope: str = "personal", reason: str = "", attempt: int | None = None) -> str | None:
    """Record one nightly-job run and return its id (None when it could not be written).
    `ok` is only "the call succeeded"; `reason` is why the row was wasted. Never raises into the caller."""
    try:
        now, uid = time.time(), uuid.uuid4().hex
        mr.append(SOURCE, JOBS, {"uid": uid, "job": job, "model": model, "ok": 1 if ok else 0, "duration_s": duration_s,
                                 "cost_usd": cost_usd, "detail": detail[:500], "scope": scope, "reason": reason[:200],
                                 "attempt": attempt}, at=now, uid=uid)
        return uid
    except Exception as exc:  # noqa: BLE001
        log.debug("fafnir: could not record job %s: %s", job, exc)
        return None


def reject_job(row_id: str | None, reason: str) -> None:
    """A job refused the output of a call that succeeded: the row stays `ok`, its spend turns wasted."""
    if row_id is None:
        return
    try:
        mr.append(SOURCE, REJECTS, {"job_uid": row_id, "reason": reason[:200]})
    except Exception as exc:  # noqa: BLE001
        log.debug("fafnir: could not mark job row %s rejected: %s", row_id, exc)


def record_call(surface: str, ok: bool, duration_s: float, cost_usd: float | None = None, model: str = "",
                detail: str = "", scope: str = "personal") -> None:
    """Record one provider call. Never raises into the caller."""
    try:
        mr.append(SOURCE, CALLS, {"surface": surface, "model": model, "ok": 1 if ok else 0, "duration_s": duration_s,
                                  "cost_usd": cost_usd, "detail": detail[:500], "scope": scope})
    except Exception as exc:  # noqa: BLE001
        log.debug("fafnir: could not record %s call: %s", surface, exc)


def _read(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def sample_usage() -> dict | None:
    """Snapshot the host usage counters (state/usage/status.json and usage.json are overwritten in
    place on every refresh, so this is their only history). One row per account."""
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
        rows.append({"ts": now, "scope": scope, "five_hour_tokens": five, "seven_day_tokens": seven, "est_cost_usd": cost})
    try:
        for r in rows:
            mr.append(SOURCE, SAMPLES, {k: v for k, v in r.items() if k != "ts"}, at=now)
    except Exception as exc:  # noqa: BLE001
        log.debug("fafnir: could not record usage sample: %s", exc)
    return rows[0] if rows else None


def _call_totals(rows: list[dict], exclude: tuple[str, ...] = ()) -> tuple[int, float]:
    """(calls, known spend) over rows already read. `exclude` prefixes drop out of the count only."""
    return (sum(1 for d in rows if not str(d.get("surface") or "").startswith(exclude)),
            float(sum(d.get("cost_usd") or 0 for d in rows)))


def call_rate(window_s: int, scope: str | None = None, exclude: tuple[str, ...] = ()) -> tuple[int, float]:
    """(calls, total known spend) recorded inside the window; `scope` None sums both accounts.
    `exclude` surface prefixes drop out of the call count only; spend still sums every row."""
    return _call_totals(_rows(CALLS, time.time() - window_s, scope), exclude)


def job_spend(days: float = 7, scope: str | None = None) -> tuple[float, int]:
    """(known spend, runs) of the nightly LLM jobs in the trailing window."""
    rows = _rows(JOBS, time.time() - days * 86400, scope)
    return float(sum(d.get("cost_usd") or 0 for d in rows)), len(rows)


def _jobs_with_waste(days: float) -> list[tuple[dict, bool, bool]]:
    """Each job call in the window with whether it is wasted and whether a job rejected its output.
    A row is wasted when the call failed or a job rejected its output; a retried attempt failed, so it is in."""
    since = time.time() - days * 86400
    rejected = {d["job_uid"] for d in _rows(REJECTS, since) if d.get("job_uid")}
    out = []
    for d in _rows(JOBS, since):
        rejects = bool(d.get("ok")) and (bool(d.get("reason")) or d.get("uid") in rejected)
        out.append((d, not d.get("ok") or bool(d.get("reason")) or d.get("uid") in rejected, rejects))
    return out


def wasted_spend(days: float = 7) -> list[dict]:
    """Per job in the trailing window: rows that failed, were retried or were rejected, and their known cost.
    `uids` names those rows, oldest first; `cause` is the newest one's reason, else its detail."""
    per_job: dict[str, list] = {}
    for d, wasted, _ in _jobs_with_waste(days):
        if wasted:
            per_job.setdefault(d["job"], []).append(d)
    rows = []
    for job, ds in per_job.items():
        ds.sort(key=lambda d: d.get("ts") or 0)
        rows.append({"job": job, "rows": len(ds), "usd": round(sum(d.get("cost_usd") or 0 for d in ds), 2),
                     "uids": [d.get("uid") for d in ds],
                     "cause": str(ds[-1].get("reason") or ds[-1].get("detail") or "")[:120]})
    return sorted(rows, key=lambda r: -r["usd"])


def wasted_line(days: int = 7) -> str:
    """One line for the digest and the report; empty when nothing was wasted."""
    rows = wasted_spend(days)
    if not rows:
        return ""
    per_job = ", ".join(f"{r['job']} ${r['usd']:.2f} ({r['rows']})" for r in rows)
    return f"wasted spend, {days} days: ${sum(r['usd'] for r in rows):.2f} in {sum(r['rows'] for r in rows)} row(s): {per_job}"


def job_spend_by_job() -> list[dict]:
    """Per job over 30 days: scope(s), runs and known spend in 7 and 30 days, failed runs, rejected runs.
    For Jarvis's job-value review and the HUD's Job value panel."""
    week = time.time() - 7 * 86400
    per_job: dict[str, list] = {}
    for d, _, rejects in _jobs_with_waste(30):
        per_job.setdefault(d["job"], []).append((d, rejects))
    out = []
    for job, rows in per_job.items():
        costs = [d["cost_usd"] for d, _ in rows if d.get("cost_usd") is not None]
        recent = [d for d, _ in rows if d["ts"] >= week]
        out.append({"job": job, "scope": ",".join(sorted({d["scope"] for d, _ in rows})), "runs7": len(recent),
                    "usd7": round(sum(d.get("cost_usd") or 0 for d in recent), 2), "runs30": len(rows),
                    "usd30": round(sum(costs), 2) if costs else None, "failed30": sum(1 for d, _ in rows if not d.get("ok")),
                    "rejected30": sum(1 for _, rejects in rows if rejects)})
    return sorted(out, key=lambda r: -(r["usd30"] or 0))


def _status_fresh(updated_at: object) -> bool:
    """True when `updated_at` is an offset ISO 8601 time no older than STATUS_MAX_AGE_S.
    A missing offset has no absolute age, so it counts as unparseable and the pause stays off."""
    if not isinstance(updated_at, str):
        return False
    try:
        stamp = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        return False
    return time.time() - stamp.timestamp() <= STATUS_MAX_AGE_S


def five_hour_tokens(scope: str = "personal") -> float | None:
    """Billable tokens in the trailing 5h window (state/usage/status.json, refreshed hourly).
    Only the personal account is measured. None means unknown, fail open (the 5h pause stays off):
    the file or the count is unreadable, or top-level `updated_at` is missing, unparseable, or
    older than STATUS_MAX_AGE_S. A stale high number must not pin the optional-job pause on."""
    if scope != "personal":
        return None
    doc = _read(usage_dir() / "status.json")
    if not _status_fresh(doc.get("updated_at")):
        return None
    try:
        return float(doc["claude"]["five_hour_billable_tokens"])
    except (KeyError, TypeError, ValueError):
        return None


def job_budget(cfg: Mapping[str, Any] | None = None, scope: str = "personal") -> dict:
    """One account's LLM-job ceilings and where its spend stands ([llm_jobs]):
    weekly ceiling (`over`), optional daily ceiling (`over_daily`, in `over`), and the optional pause
    at the weekly ceiling (OPTIONAL_PAUSE_FRACTION, 100% since 2026-10-20), the daily ceiling or the 5h token pause
    (`paused_for_optional`)."""
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


def calls_tag(scope: str) -> str:
    """Prefix of the call-count finding: it warns only, and the brake counts `burn_tag` findings alone."""
    return burn_tag(scope).replace("BURN", "BURN-CALLS", 1)


def _top_surfaces(rows: list[dict], *, by_count: bool, exclude: tuple[str, ...] = ()) -> str:
    """` Top: name N calls $X; ...` for the three busiest surfaces in one scope's window (rows already read).

    Call-count rankings drop `exclude` prefixes, same as the call count. Spend rankings keep every
    surface, because those calls still count toward spend. Ties break on the other figure, then the name."""
    totals: dict[str, tuple[int, float]] = {}
    for d in rows:
        name = str(d.get("surface") or "")
        if by_count and name.startswith(exclude):
            continue
        calls, spend = totals.get(name, (0, 0.0))
        totals[name] = (calls + 1, spend + (d.get("cost_usd") or 0))

    def order(item: tuple[str, tuple[int, float]]) -> tuple:
        name, (calls, spend) = item
        return ((-calls, -spend) if by_count else (-spend, -calls)) + (name,)

    parts = [f"{name} {calls} calls ${spend:.2f}" for name, (calls, spend) in sorted(totals.items(), key=order)[:3]]
    return (" Top: " + "; ".join(parts)) if parts else ""


def burn_findings(cfg: Mapping[str, Any] | None = None) -> list[tuple[str, str]]:
    """Over-limit spending per account in the burn window, each with a stable alert key.
    A key names the condition; only the message carries the numbers."""
    s = cfg_burn(cfg)
    if not s["enabled"]:
        return []
    window, max_calls, max_spend = int(s["window_s"]), int(s["max_calls_per_window"]), float(s["max_spend_usd_per_window"])
    exclude = tuple(s["count_exclude_surfaces"])
    out = []
    for scope in SCOPES:  # each account on its own: a burst on one says nothing about the other
        rows = _rows(CALLS, time.time() - window, scope)  # once: the totals and the top surfaces share it
        calls, spend = _call_totals(rows, exclude)
        if calls > max_calls:
            out.append((f"burn:calls:{scope}", f"{calls_tag(scope)} the daemon made {calls} {scope}-account call(s) in "
                                                f"{window // 60} min, over the limit of {max_calls} (warning only)"
                                                f"{_top_surfaces(rows, by_count=True, exclude=exclude)}"))
        if spend > max_spend:
            out.append((f"burn:spend:{scope}", f"{burn_tag(scope)} the daemon spent ${spend:.2f} on the {scope} account "
                                                f"in {window // 60} min, over the limit of ${max_spend:.2f}"
                                                f"{_top_surfaces(rows, by_count=False)}"))
    return out


def report_lines(cfg: Mapping[str, Any] | None = None) -> list[str]:
    s = cfg_burn(cfg)
    window = int(s["window_s"])
    lines = []
    for scope in SCOPES:
        calls, spend = call_rate(window, scope, tuple(s["count_exclude_surfaces"]))
        day_calls, day_spend = call_rate(86400, scope)
        b = job_budget(cfg, scope)
        lines += [f"  [{scope}] last {window // 60} min : {calls} call(s), ${spend:.2f} known spend "
                  f"(limits {s['max_calls_per_window']} / ${s['max_spend_usd_per_window']:.2f})",
                  f"  [{scope}] last 24h     : {day_calls} call(s), ${day_spend:.2f} known spend",
                  f"  [{scope}] LLM jobs 7d  : ${b['spent']:.2f} of ${b['ceiling']:.2f} in {b['runs']} run(s)"]
    if wasted := wasted_line():
        lines.append(f"  {wasted}")
    return lines
