#!/Users/anzes/.budget/.venv/bin/python
"""Weekly context audit (2026-10-18, `budget context-audit`, Jarvis job `context-audit`).

Measures what long interactive sessions cost by re-reading their own context, from the Claude
Code transcripts of both configs (the `usage` of every assistant turn): the cache-read spend, the
part of it spent re-reading context above WINDOW tokens (the auto-compact window both configs set
on 2026-10-18), how many sessions passed it, how often they compacted, and the costliest sessions.
Baseline before the fix (week to 2026-10-18): $3,048 cache-read, $1,241 above 200k in 51% of turns.
List-price estimates (the subscription bills quota, not dollars): use them to compare weeks.
Writes state/reports/context/<date>.md and latest.md, and the receipt state/jobs/context-audit.json.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fafnir import store  # noqa: E402

HOME = Path.home()
CONFIGS = {"personal": HOME / ".claude-personal" / "projects", "work": HOME / ".claude-work" / "projects"}
WINDOW = int(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or 200_000)
CACHE_READ_USD = {"opus": 0.5, "sonnet": 0.3, "haiku": 0.1}  # per million tokens, list price
BASELINE = {"week_to": "2026-10-18", "cache_read_usd": 3048, "above_window_usd": 1241, "turn_share_above": 0.51}


def audit(days: int = 7) -> dict:
    cut = time.time() - days * 86400
    sess: dict = defaultdict(lambda: {"turns": 0, "cr_usd": 0.0, "above_usd": 0.0, "max_ctx": 0, "compactions": 0, "cfg": ""})
    turns = above_turns = 0
    seen = set()  # one transcript line per content block repeats the message usage
    for cfg, root in CONFIGS.items():
        for f in root.glob("*/*.jsonl"):
            try:
                if f.stat().st_mtime < cut:
                    continue
                lines = f.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            s = sess[f.stem]
            s["cfg"] = cfg
            with lines:
                for line in lines:
                    if '"compact_boundary"' in line:
                        s["compactions"] += 1
                        continue
                    if '"usage"' not in line or '"assistant"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                        m = rec["message"]
                        u = m["usage"]
                    except (ValueError, KeyError, TypeError):
                        continue
                    key = (m.get("id"), rec.get("requestId"))
                    if key[0] and key in seen:
                        continue
                    seen.add(key)
                    price = next((v for k, v in CACHE_READ_USD.items() if k in str(m.get("model"))), 0.3)
                    cr = int(u.get("cache_read_input_tokens") or 0)
                    ctx = cr + int(u.get("cache_creation_input_tokens") or 0) + int(u.get("input_tokens") or 0)
                    s["turns"] += 1
                    s["cr_usd"] += cr * price / 1e6
                    s["max_ctx"] = max(s["max_ctx"], ctx)
                    turns += 1
                    if ctx > WINDOW:
                        above_turns += 1
                        s["above_usd"] += (ctx - WINDOW) * price / 1e6
    active = {k: v for k, v in sess.items() if v["turns"]}
    top = sorted(active.items(), key=lambda kv: -kv[1]["cr_usd"])[:8]
    return {"days": days, "window": WINDOW, "sessions": len(active), "turns": turns,
            "cache_read_usd": round(sum(v["cr_usd"] for v in active.values()), 2),
            "above_window_usd": round(sum(v["above_usd"] for v in active.values()), 2),
            "turn_share_above": round(above_turns / turns, 3) if turns else 0.0,
            "sessions_above": sum(1 for v in active.values() if v["max_ctx"] > WINDOW),
            "compactions": sum(v["compactions"] for v in active.values()),
            "top": [{"session": k[:8], "config": v["cfg"], "turns": v["turns"], "max_ctx_k": v["max_ctx"] // 1000,
                     "cache_read_usd": round(v["cr_usd"], 2), "above_usd": round(v["above_usd"], 2),
                     "compactions": v["compactions"]} for k, v in top], "baseline": BASELINE}


def render(a: dict) -> str:
    b = a["baseline"]
    lines = [f"# Context audit, {date.today().isoformat()} (last {a['days']} days)", "",
             f"Auto-compact window: {a['window'] // 1000}k tokens. List-price estimates; compare weeks, do not bill.", "",
             "| | this week | baseline (week to " + b["week_to"] + ") |", "|---|--:|--:|",
             f"| cache-read spend | ${a['cache_read_usd']:,.0f} | ${b['cache_read_usd']:,} |",
             f"| re-reading context above the window | ${a['above_window_usd']:,.0f} | ${b['above_window_usd']:,} |",
             f"| turns above the window | {a['turn_share_above']:.0%} | {b['turn_share_above']:.0%} |", "",
             f"{a['sessions']} sessions, {a['turns']} turns, {a['sessions_above']} sessions passed the window, "
             f"{a['compactions']} compactions.", "", "## Costliest sessions", "",
             "| session | config | turns | max context | cache read | above window | compactions |", "|---|---|--:|--:|--:|--:|--:|"]
    lines += [f"| {t['session']} | {t['config']} | {t['turns']} | {t['max_ctx_k']}k | ${t['cache_read_usd']:.0f} | "
              f"${t['above_usd']:.0f} | {t['compactions']} |" for t in a["top"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    a = audit()
    out = store.STATE / "reports" / "context"
    out.mkdir(parents=True, exist_ok=True)
    text = render(a)
    (out / f"{date.today().isoformat()}.md").write_text(text, encoding="utf-8")
    (out / "latest.md").write_text(text, encoding="utf-8")
    (store.STATE / "jobs").mkdir(parents=True, exist_ok=True)
    (store.STATE / "jobs" / "context-audit.json").write_text(json.dumps({"ran_at": time.time(), "ok": True, **a}, indent=2))
    print(f"context audit: ${a['above_window_usd']:,.0f} re-reading above {a['window'] // 1000}k "
          f"(baseline ${BASELINE['above_window_usd']:,}), {a['turn_share_above']:.0%} of turns above")
    return 0


if __name__ == "__main__":
    sys.exit(main())
