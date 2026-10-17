#!/Users/anzes/.budget/.venv/bin/python
"""Billable tokens for the last N days, split by model and by effort level.

Reads the Claude Code transcripts the same way `~/.claude-personal/scripts/
token-report.py` does: every assistant line with a `usage` block counts once.
Billable is input + output + cache_creation, the same definition, so the two
reports agree on the total. Cache reads are shown separately and not billed
here, for the same reason token-report gives: they would swamp the picture.

`effort` is read from the assistant line itself (the level the request ran at).
Lines without one are counted under "(none)": that is the session default, or a
model with no effort setting.

Why this exists: model-router picks a tier and an effort per prompt. This shows
whether that choice moves spend, and where it goes. Detection only.

Output: reports/spend/by-model-effort.md and state/spend-by-model-effort.json.
Usage: spend-by-effort.py [--days N]
"""
from __future__ import annotations
import sys as _sys
if _sys.version_info < (3, 11):  # /usr/bin/python3 is 3.9 (no tomllib); the daemon runs .venv (3.13)
    raise SystemExit(f"{__file__}: needs Python 3.11+, got {_sys.version.split()[0]}; "
                     "run it with ~/.budget/.venv/bin/python")

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
B = HOME / ".budget"
REPORT = B / "state" / "reports" / "spend" / "by-model-effort.md"
ARTIFACT = B / "state" / "spend-by-model-effort.json"
PROJECT_DIRS = [Path(os.path.expanduser(p)) for p in ("~/.claude-work/projects", "~/.claude-personal/projects")]


def parse_ts(rec: dict) -> datetime | None:
    raw = rec.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def collect(days: int, now: datetime) -> dict:
    cutoff = now - timedelta(days=days)
    # (model, effort) -> counters
    cells: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for d in PROJECT_DIRS:
        if not d.exists():
            continue
        for path in d.glob("*/*.jsonl"):
            if path.stat().st_mtime < cutoff.timestamp():
                continue  # file untouched since before the window
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ts = parse_ts(rec)
                    if rec.get("type") != "assistant" or ts is None or ts < cutoff:
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    key = (msg.get("model") or "unknown", str(rec.get("effort") or "(none)"))
                    c = cells[key]
                    c["input"] += int(usage.get("input_tokens") or 0)
                    c["output"] += int(usage.get("output_tokens") or 0)
                    c["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
                    c["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
                    c["requests"] += 1
    return cells


def billable(c: dict[str, int]) -> int:
    return c["input"] + c["output"] + c["cache_write"]


def render(cells: dict, days: int, now: datetime) -> str:
    total = sum(billable(c) for c in cells.values()) or 1
    lines = [
        f"# Billable tokens by model and effort - last {days} days",
        "",
        f"Generated {now.astimezone().strftime('%Y-%m-%d %H:%M')}. Billable = input + output + cache write; "
        "cache reads excluded (same as token-report). Requests counted per assistant message.",
        "",
        "| model | effort | requests | billable | share | cache read |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for (model, effort), c in sorted(cells.items(), key=lambda kv: -billable(kv[1])):
        b = billable(c)
        lines.append(f"| {model} | {effort} | {c['requests']} | {b:,} | {100 * b / total:.1f}% | {c['cache_read']:,} |")
    if not cells:
        lines.append("| - | - | 0 | 0 | - | - |")
    by_effort: dict[str, int] = defaultdict(int)
    for (_, effort), c in cells.items():
        by_effort[effort] += billable(c)
    lines += ["", "By effort: " + ", ".join(f"{e} {100 * v / total:.1f}%" for e, v in
                                             sorted(by_effort.items(), key=lambda kv: -kv[1])) if cells else ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    cells = collect(args.days, now)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(render(cells, args.days, now), encoding="utf-8")
    ARTIFACT.write_text(json.dumps({
        "checked_at": now.isoformat(timespec="seconds"),
        "days": args.days,
        "billable_total": sum(billable(c) for c in cells.values()),
        "cells": len(cells),
    }, indent=2) + "\n", encoding="utf-8")
    print(f"spend-by-effort: {len(cells)} model/effort cell(s), {args.days}d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
