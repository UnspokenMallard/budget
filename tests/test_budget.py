"""Budget against a scratch store: ledger and ceilings, the brake decision, usage limits, the
service's answers and its write guard, and the CLI exit codes job scripts rely on.
Run: ~/.budget/.venv/bin/python ~/.budget/tests/test_budget.py"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="budget-test-"))
os.environ["BUDGET_HOME"] = str(TMP)  # read at import: never the live store
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

from budget import brake, ledger, limits, store  # noqa: E402

CFG = {"llm_jobs": {"weekly_budget_usd": 10.0, "daily_budget_usd": 6.0},
       "burn": {"max_calls_per_window": 2, "brake_after_consecutive_windows": 2, "brake_clears_after_s": 1}}


def test_ledger_and_ceilings():
    assert store.DB_PATH == TMP / "state" / "budget.db"
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
    assert [k for k, _ in ledger.burn_findings(CFG)] == ["burn:calls:personal"]
    j = ledger.job_spend_by_job()[0]
    assert j["job"] == "advisor" and j["runs30"] == 2 and j["usd30"] == 6.5 and j["failed30"] == 0


def test_brake():
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
    cli = [str(ROOT / "bin" / "budget")]
    env = {**os.environ, "BUDGET_HOME": str(TMP)}
    run = lambda *a: subprocess.run(cli + list(a), capture_output=True, text=True, env=env)  # noqa: E731
    assert run("record-job", "nightly", "1", "3", "0.25", "haiku", "a detail").returncode == 0
    assert run("job-budget").returncode == 0  # no config.toml in the scratch home: the default $40 ceiling, not reached
    assert run("job-budget", "--scope=nobody").returncode == 2
    # token-report reads transcripts under $HOME: one assistant turn, then the dated report and its receipt
    proj = TMP / "home" / ".claude-personal" / "projects" / "p"
    proj.mkdir(parents=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (proj / "s.jsonl").write_text(json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "model": "claude-haiku-5-5", "usage": {"input_tokens": 10, "output_tokens": 5}, "content": []}}) + "\n")
    r = subprocess.run([sys.executable, str(ROOT / "budget" / "jobs" / "token-report.py")], capture_output=True, text=True, env={**env, "HOME": str(TMP / "home")})
    receipt = json.loads((TMP / "state" / "jobs" / "token-report.json").read_text())
    assert r.returncode == 0 and receipt["ok"] and receipt["bytes"] >= 200, r.stdout + r.stderr
    assert Path(receipt["report"]).parent == TMP / "state" / "reports" / "token-attribution"


if __name__ == "__main__":
    test_ledger_and_ceilings()
    test_brake()
    test_limits()
    test_service_and_cli()
    print("ok")
