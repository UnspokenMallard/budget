#!/Users/anzes/.budget/.venv/bin/python
"""Today's usage digest row (`budget usage-digest`, 2026-10-18; Jarvis job `usage-digest`).

The measuring half of Jarvis's old run-usage-digest.py: read state/usage/status.json and usage.json
(usage-refresh writes both) and write the row Jarvis puts into the "Claude / Cursor Usage Digest"
Notion table, as state/reports/usage-digest/<date>.json and latest.json. Posting stays Jarvis's.
The Note column ends with the 'wasted spend, 7 days' line (2026-10-22). The Cursor column reads "-" since Cursor was removed on 2026-10-01.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from budget import ledger, store  # noqa: E402

HEADER = ["Date", "Claude 5h tokens", "Claude 7d tokens", "Claude est. cost (USD)", "Cursor used %", "Note"]


def build_row(usage_path: Path, status_path: Path) -> tuple[str, list[str]]:
    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    date = usage.get("window") or datetime.now().date().isoformat()
    cost = (usage.get("claude") or {}).get("estimatedCostUsd")
    five = seven = None
    note = "status.json billable_tokens"
    try:
        claude = json.loads(status_path.read_text(encoding="utf-8")).get("claude") or {}
        five, seven = claude.get("five_hour_billable_tokens"), claude.get("seven_day_billable_tokens")
    except (OSError, ValueError):
        pass
    if five is None or seven is None:
        c = usage.get("claude") or {}
        total = sum(int(c.get(k) or 0) for k in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"))
        five = five if five is not None else total
        seven = seven if seven is not None else total
        note = "fallback: usage.json token sum (status.json missing or incomplete)"
    return date, [date, str(five), str(seven), f"${cost:.4f}" if cost is not None else "-", "-", note]


def main() -> int:
    usage = store.STATE / "usage" / "usage.json"
    if not usage.exists():
        print(f"{usage} does not exist - usage-refresh may not have run yet")
        return 1
    try:
        date, values = build_row(usage, store.STATE / "usage" / "status.json")
    except (OSError, ValueError) as exc:
        print(f"usage digest row not built: {type(exc).__name__}: {exc}")
        return 1
    try:
        wasted = ledger.wasted_line()
    except Exception as exc:  # noqa: BLE001 - the usage row is worth posting without it
        wasted = ""
        print(f"wasted spend not read: {type(exc).__name__}: {exc}")
    if wasted:
        values[-1] += f"; {wasted}"
    out = store.STATE / "reports" / "usage-digest"
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                       "date": date, "header": HEADER, "row": values, "wasted": wasted}, indent=2)
    (out / f"{date}.json").write_text(text, encoding="utf-8")
    (out / "latest.json").write_text(text, encoding="utf-8")
    print(f"usage digest row for {date}: {', '.join(values[1:4])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
