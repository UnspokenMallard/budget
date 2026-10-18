"""`budget` CLI (bin/budget). Same contract as Jarvis's old `burn.py` CLI, so job scripts keep it:

  budget record-job <name> <ok 0|1> <duration_s> <cost_usd|-> <model> [detail...] [--scope=work]
  budget job-budget [--optional] [--scope=work]   exit 0 under, 1 at or over, 2 could not check
  budget usage-refresh                            refresh state/usage/status.json and usage.json
  budget spend-by-effort [--days N]
  budget context-audit                            weekly: what long sessions spend re-reading their context
  budget token-report [--days N]                  weekly: token attribution report and its receipt
  budget limits                                   one line per provider
  budget report
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import brake, ledger, limits

JOBS = Path(__file__).resolve().parent / "jobs"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    scope = "personal"
    for arg in list(argv):
        if arg.startswith("--scope="):
            scope = arg.split("=", 1)[1]
            argv.remove(arg)
    if scope not in ledger.SCOPES:
        print(f"unknown scope {scope!r}")
        return 2
    cmd = argv[:1]
    if cmd == ["record-job"] and len(argv) >= 6:
        name, ok, duration, cost, model = argv[1:6]
        ledger.record_job(name, ok=ok == "1", duration_s=float(duration),
                          cost_usd=None if cost in ("", "-") else float(cost), model=model,
                          detail=" ".join(argv[6:]), scope=scope)
        return 0
    if cmd == ["job-budget"]:
        try:
            b = ledger.job_budget(scope=scope)
        except Exception as exc:  # noqa: BLE001 - the caller must see any failure
            print(f"could not check the LLM job budget: {type(exc).__name__}: {exc}")
            return 2
        none = lambda v: "none" if v is None else v  # noqa: E731
        print(f"scope={scope} spent={b['spent']:.2f} ceiling={b['ceiling']:.2f} runs={b['runs']} "
              f"day={b['spent_day']:.2f}/{none(b['daily_ceiling'])} "
              f"5h={'n/a' if b['five_hour_tokens'] is None else b['five_hour_tokens']}/{none(b['five_hour_pause_tokens'])}")
        if "--optional" in argv:  # optional jobs stop before the ceiling, so the weekly budget stays for the rest
            return 1 if b["paused_for_optional"] else 0
        return 1 if b["over"] else 0
    if cmd in (["usage-refresh"], ["spend-by-effort"], ["context-audit"], ["token-report"]):
        script = {"usage-refresh": "update-usage-job.py", "spend-by-effort": "spend-by-effort.py",
                  "context-audit": "context-audit.py", "token-report": "token-report.py"}[cmd[0]]
        return subprocess.run([sys.executable, str(JOBS / script), *argv[1:]]).returncode
    if cmd == ["limits"]:
        print("limits\n" + limits.status())
        return 0
    lines = ["budget report", "", *ledger.report_lines()]
    for s in ledger.SCOPES:
        on, state = brake.engaged(scope=s)
        lines.append(f"  [{s}] brake       : {'ENGAGED until ' + state['clears_at_human'] if on else 'off'}")
    print("\n".join(lines) + "\n\n" + limits.status())
    return 0
