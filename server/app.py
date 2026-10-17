"""Budget's HTTP service on 127.0.0.1:3025 (launchd com.budget.server), for callers that are not
Python on this Mac: the HUD records its `claude -p` calls here and reads the gate, the limits and
job spend. Python callers import the `budget` library directly (same code, same store).

Localhost only: the host check refuses DNS rebinding, and a write carrying an Origin header (any
browser page) is refused, so no web page can write to the ledger through Anze's browser.
Budget measures and answers; it never alerts or holds work.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from budget import brake, ledger, limits, quota  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

HOST, PORT = "127.0.0.1", 3025


def scope_of(r: Request) -> str:
    s = r.path_params.get("scope") or r.query_params.get("scope") or "personal"
    if s not in ledger.SCOPES:
        raise ValueError(f"unknown scope {s!r}")
    return s


def get(fn):
    async def endpoint(r: Request):
        try:
            return JSONResponse(fn(r))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    return endpoint


def post(fn):
    async def endpoint(r: Request):
        if r.headers.get("origin"):
            return JSONResponse({"error": "writes are not accepted from a browser page"}, status_code=403)
        try:
            body = await r.json()
            fn(body if isinstance(body, dict) else {})
            return JSONResponse({"ok": True})
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    return endpoint


def budget_answer(r: Request) -> dict:
    b = ledger.job_budget(scope=scope_of(r))
    optional = r.query_params.get("optional") in ("1", "true")
    return {**b, "allowed": not (b["paused_for_optional"] if optional else b["over"]), "optional": optional}


def record_call(b: dict) -> None:
    ledger.record_call(str(b["surface"]), bool(b.get("ok")), float(b.get("duration_s") or 0),
                       None if b.get("cost_usd") is None else float(b["cost_usd"]), str(b.get("model") or ""),
                       str(b.get("detail") or ""), b.get("scope") if b.get("scope") in ledger.SCOPES else "personal")


def record_job(b: dict) -> None:
    ledger.record_job(str(b["job"]), bool(b.get("ok")), float(b.get("duration_s") or 0),
                      None if b.get("cost_usd") is None else float(b["cost_usd"]), str(b.get("model") or ""),
                      str(b.get("detail") or ""), b.get("scope") if b.get("scope") in ledger.SCOPES else "personal")


def limits_answer(_r) -> dict:
    out = {}
    for p in limits.PROVIDERS:
        blocked, until = limits.blocked(p)
        out[p] = {"blocked": blocked, "until": until}
    return out


def health(_r) -> dict:
    return {"ok": True, "db": str(ledger.store.DB_PATH),
            "brake": {s: brake.engaged(scope=s)[0] for s in ledger.SCOPES},
            "budget": {s: {k: v for k, v in ledger.job_budget(scope=s).items() if k in ("spent", "ceiling", "over")}
                       for s in ledger.SCOPES}}


routes = [
    Route("/health", get(health)),
    Route("/budget/{scope}", get(budget_answer)),
    Route("/limits", get(limits_answer)),
    Route("/quota", get(lambda r: dict(zip(("low", "why"), quota.low_headroom())))),
    Route("/rate", get(lambda r: dict(zip(("calls", "spend"), ledger.call_rate(int(r.query_params.get("window_s", 900)),
                                                                                r.query_params.get("scope")))))),
    Route("/brake/{scope}", get(lambda r: dict(zip(("engaged", "state"), brake.engaged(scope=scope_of(r)))))),
    Route("/spend/jobs", get(lambda r: ledger.job_spend_by_job())),
    Route("/usage", get(lambda r: {"status": ledger._read(ledger.usage_dir() / "status.json"),
                                   "usage": ledger._read(ledger.usage_dir() / "usage.json")})),
    Route("/record/call", post(record_call), methods=["POST"]),
    Route("/record/job", post(record_job), methods=["POST"]),
]

app = Starlette(routes=routes, middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
