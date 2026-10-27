"""Fafnir against a scratch Mimir store: ledger and ceilings, the brake decision, usage limits, the
service's answers and its write guard, and the CLI exit codes job scripts rely on.
Run: ~/.budget/.venv/bin/python ~/.budget/tests/test_fafnir.py"""
import json
import os
import runpy
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="fafnir-test-"))
os.environ["FAFNIR_HOME"] = str(TMP)  # read at import: never the live store
os.environ["MEMORY_DB"] = str(TMP / "memory.db")  # Mimir's store, a scratch one
os.environ["MIMIR_OUTBOX"] = str(TMP / "outbox")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path.home() / ".mind" / "mimir"))

import mem  # noqa: E402
import mimirrecords as mr  # noqa: E402

mr.use_local(mem.connect(shared=True))  # calls go to the scratch store in-process, no HTTP

from fafnir import brake, cli, ledger, limits, store  # noqa: E402

CFG = {"llm_jobs": {"weekly_budget_usd": 10.0, "daily_budget_usd": 6.0},
       "burn": {"max_calls_per_window": 2, "max_spend_usd_per_window": 1.0, "brake_after_consecutive_windows": 2, "brake_clears_after_s": 1}}


def test_ledger_and_ceilings():
    assert not hasattr(store, "DB_PATH")  # no database of its own: the ledger is Mimir telemetry
    ledger.record_job("advisor", True, 10, 5.0, "sonnet")
    b = ledger.job_budget(CFG)
    assert not b["over"] and b["paused_for_optional"] is False and b["spent"] == 5.0
    ledger.record_job("advisor", True, 10, 1.5, "sonnet")
    b = ledger.job_budget(CFG)
    assert b["over"] and b["over_daily"]  # 6.5 >= the daily 6.0
    assert ledger.job_budget(CFG, "work")["spent"] == 0  # each account its own ledger
    for _ in range(3):
        ledger.record_call("criteria-score", True, 1.0, None, "haiku")  # unknown price still counts
    assert ledger.call_rate(900, "personal") == (3, 0.0)
    ledger.record_call("hud-whereami", True, 3.0, 0.25, "haiku")
    assert ledger.call_rate(900, "personal", ("hud-",)) == (3, 0.25)  # not counted, spend still is
    assert [k for k, _ in ledger.burn_findings(CFG)] == ["burn:calls:personal"]
    j = ledger.job_spend_by_job()[0]
    assert j["job"] == "advisor" and j["runs30"] == 2 and j["usd30"] == 6.5 and j["failed30"] == 0


def test_wasted_spend():
    # a row imported from the old budget.db: every column copied, a NULL reason, no uid
    mr.append("fafnir", ledger.JOBS, {"id": 7, "job": "legacy", "ok": 0, "cost_usd": 1.0, "scope": "personal", "reason": None},
              at=time.time())
    assert ledger.wasted_spend() == [{"job": "legacy", "rows": 1, "usd": 1.0}]  # NULL reason, ok=0 still counts
    ledger.record_job("w-job", True, 1, 0.5, "haiku", attempt=1)  # useful
    bad = ledger.record_job("w-job", True, 1, 0.25, "haiku", attempt=1)
    ledger.reject_job(bad, "no artifact")  # call ok, output rejected
    ledger.record_job("w-job", False, 1, 0.125, "haiku", reason="transient", attempt=2)  # retried
    assert [w for w in ledger.wasted_spend() if w["job"] == "w-job"] == [{"job": "w-job", "rows": 2, "usd": 0.38}]
    j = next(x for x in ledger.job_spend_by_job() if x["job"] == "w-job")
    assert j["failed30"] == 1 and j["rejected30"] == 1
    assert ledger.wasted_line().startswith("wasted spend, 7 days: $")
    assert isinstance(bad, str) and ledger.record_job("later", True, 1)  # an id is what reject_job takes back


def test_brake():
    findings = [t for _, t in ledger.burn_findings(CFG)]
    assert findings and findings[0].startswith("BURN-CALLS:") and "warning only" in findings[0]
    assert brake.consider(findings, CFG) == (None, None)  # call count over the limit only warns
    assert brake.consider(findings, CFG) == (None, None)
    ledger.record_call("hud", True, 1.0, 1.2, "opus")  # spend $1.2 > $1.0 per window
    assert [k for k, _ in ledger.burn_findings(CFG)] == ["burn:calls:personal", "burn:spend:personal"]
    findings = [t for _, t in ledger.burn_findings(CFG)]
    note, engaged = brake.consider(findings, CFG)
    assert engaged is None and "1/2" in note
    note, engaged = brake.consider(findings, CFG)
    assert engaged and engaged["scope"] == "personal" and brake.engaged(CFG)[0]
    assert brake.consider(findings, CFG) == ("already braked", None)  # alerted once, not every window
    time.sleep(1.1)
    assert brake.engaged(CFG) == (False, None)  # self-clears
    assert brake.consider([], CFG) == (None, None)


def test_limits():
    assert limits.detect("artifact usage.json fresh (0.0m old, 429 B)") is None  # a byte count is not a limit
    hit = limits.detect("Claude AI usage limit reached|1792240000", limits.CLAUDE)
    assert hit is not None
    limits.record(hit)
    key, message, severity, _ = limits.alert(hit.provider)
    assert key == "claude-usage-limit" and severity == "warn" and "deferred" in message
    assert limits.blocked(limits.CLAUDE)[0] and (TMP / "state" / "claude-limit.json").exists()
    limits.note_success(limits.CLAUDE)
    assert limits.blocked(limits.CLAUDE) == (False, None)


def test_billable_tokens_count_a_message_once():
    d = TMP / "projects"
    (d / "p").mkdir(parents=True)
    rec = {"type": "assistant", "requestId": "r1", "message": {"id": "m1", "usage": {"input_tokens": 2, "output_tokens": 8, "cache_creation_input_tokens": 90}}}
    (d / "p" / "s.jsonl").write_text("\n".join(json.dumps(r) for r in (rec, rec)) + "\n")  # thinking + tool_use blocks
    old = limits.PROJECTS_DIRS_BY_PROVIDER[limits.CLAUDE]
    limits.PROJECTS_DIRS_BY_PROVIDER[limits.CLAUDE] = [d]
    try:
        assert limits._billable_tokens_since(0) == 100
    finally:
        limits.PROJECTS_DIRS_BY_PROVIDER[limits.CLAUDE] = old


def test_service_and_cli():
    import app
    from starlette.testclient import TestClient
    c = TestClient(app.app, base_url="http://127.0.0.1:3025")
    assert c.get("/health").json()["ok"]
    g = c.get("/budget/personal?optional=1").json()
    assert set(g) >= {"allowed", "spent", "ceiling", "paused_for_optional"}
    assert c.get("/budget/nobody").status_code == 400
    assert c.post("/record/call", json={"surface": "hud", "ok": True, "duration_s": 2, "cost_usd": 0.01}).json() == {"ok": True}
    assert c.post("/record/call", json={"surface": "x"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert c.post("/record/call", json={}).status_code == 400
    assert TestClient(app.app, base_url="http://evil.example").get("/health").status_code == 400
    assert cli.main(["job-budget"]) == 0  # no config.toml in the scratch home: the default ceiling, not reached
    assert cli.main(["job-budget", "--scope=nobody"]) == 2
    # the budget command with Mimir off: a record is queued in the outbox (exit 0), a read says so (2 and 3)
    env = {**os.environ, "FAFNIR_HOME": str(TMP), "MIMIR_URL": "none"}
    run = lambda *a: subprocess.run([str(ROOT / "bin" / "budget"), *a], capture_output=True, text=True, env=env)  # noqa: E731
    assert run("record-job", "nightly", "1", "3", "0.25", "haiku", "a detail").returncode == 0
    assert run("record-job", "cli-job", "0", "3", "0.5", "haiku", "d", "--reason=error_max_turns", "--attempt=2").returncode == 0
    assert run("job-budget").returncode == 2 and run("report").returncode == 3
    assert mr.pending("fafnir")["pending"] == 2  # what Mimir missed is waiting, not lost
    assert any(w["job"] == "cli-job" for w in ledger.wasted_spend())  # Mimir is back: a read replays the outbox first
    assert mr.pending("fafnir") == {"pending": 0, "oldest_age_s": 0, "rejected": 0}
    # token-report reads transcripts under $HOME: one assistant turn, then the dated report and its receipt
    proj = TMP / "home" / ".claude-personal" / "projects" / "p"
    proj.mkdir(parents=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (proj / "s.jsonl").write_text(json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "model": "claude-haiku-5-5", "usage": {"input_tokens": 10, "output_tokens": 5}, "content": []}}) + "\n")
    r = subprocess.run([sys.executable, str(ROOT / "fafnir" / "jobs" / "token-report.py")], capture_output=True, text=True, env={**env, "HOME": str(TMP / "home"), "PYTHONPATH": str(Path.home() / ".mind" / "mimir")})
    receipt = json.loads((TMP / "state" / "jobs" / "token-report.json").read_text())
    assert r.returncode == 0 and receipt["ok"] and receipt["bytes"] >= 200, r.stdout + r.stderr
    assert Path(receipt["report"]).parent == TMP / "state" / "reports" / "token-attribution"
    # usage-digest: no usage.json is a failure; with one (and no status.json) the token-sum fallback row
    digest = [sys.executable, str(ROOT / "fafnir" / "jobs" / "usage-digest.py")]
    def run_digest():  # in process: the digest reads the ledger, which the scratch store holds
        try:
            runpy.run_path(digest[1], run_name="__main__")
        except SystemExit as exc:
            return exc.code or 0
    assert run_digest() == 1
    (TMP / "state" / "usage").mkdir(parents=True, exist_ok=True)
    (TMP / "state" / "usage" / "usage.json").write_text(json.dumps(
        {"window": "2026-10-18", "claude": {"estimatedCostUsd": 1.5, "inputTokens": 7, "outputTokens": 3}}))
    assert run_digest() == 0
    row = json.loads((TMP / "state" / "reports" / "usage-digest" / "latest.json").read_text())
    assert row["row"][:5] == ["2026-10-18", "10", "10", "$1.5000", "-"] and row["row"][5].startswith("fallback")
    assert "wasted spend, 7 days" in row["row"][5] and row["wasted"] in row["row"][5]


if __name__ == "__main__":
    tests = [test_ledger_and_ceilings, test_wasted_spend, test_brake, test_limits,
             test_billable_tokens_count_a_message_once, test_service_and_cli]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} passed")
