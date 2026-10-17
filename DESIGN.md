# Budget

What model work costs and whether more may start: the spend ledger, the ceilings, usage-limit
windows, quota headroom and the burn brake's decision. Split out of Jarvis on 2026-10-17 so
Jarvis is an orchestrator (Anze: "I want Jarvis to be an orchestrator").

## Decisions (Anze, 2026-10-17)

| Question | Decision |
|---|---|
| Home | `~/.budget`, own git repo, code and `state/` together, own venv (like `~/.mind`) |
| Schedule | Jarvis triggers; Budget has no timers. Jarvis's scheduler calls Budget's CLI or API |
| Process | HTTP service on `127.0.0.1:3025` (launchd `com.budget.server`): many callers ask it live |
| Sessions | Designed with Sentinel, Herald and Forge; Budget built first, in the same session |

## Boundary

Budget **measures and answers**: what was spent, by whom, against which ceiling; is a provider
in a usage-limit window; is there headroom; should the brake be on. It never notifies, pings,
holds work or files findings. Jarvis asks it and acts: holds the queue, defers a job, alerts.

## What moves here

| From Jarvis | Here |
|---|---|
| `daemon/burn.py` metering: `record_job`, `record_call`, `sample_usage`, `call_rate`, `job_spend`, `five_hour_tokens`, `job_budget`, the burn-rate findings, the brake state and its consecutive-window count | `budget/ledger.py`, `budget/brake.py` |
| `daemon/limits.py` (a usage limit is a defer, not a failure: detection, windows, observed ceilings) | `budget/limits.py` |
| `daemon/quota.py` (`low_headroom`) | `budget/quota.py` |
| `scripts/update-usage.py`, `update_status.py`, `update-usage-job.py` and `usage/` (status.json, usage.json, status-config.json) | `budget/usage/`, `state/usage/` |
| `scripts/run-usage-digest.py`, `scripts/spend-by-effort.py`, the `token-report` handler's work | `budget/reports/` |
| tables `provider_calls`, `job_calls`, `usage_samples` in `jarvis.db` | `state/budget.db` (copied with their rows) |
| config `[burn]` spend keys, `[llm_jobs]`, `[quota]`, `[limits]` | `~/.budget/config.toml` |

Stays in Jarvis: the stuck-event and repeated-failure detectors (they read Jarvis's own event
queue; `burn.py` shrinks to them, renamed `stuck.py`), `brain.py` and its private cap ledger
`brain_calls` (brain still records each call to Budget), the alert on brake engagement, and
`earned.py` (goes to Forge).

## Interface

Service, JSON:
- `GET /health`
- `GET /budget/{scope}?optional=1` -> `{allowed, over, over_daily, paused_for_optional, spent, ceiling, spent_day, daily_ceiling, five_hour_tokens, ...}`
- `POST /record/job` `{job, ok, duration_s, cost_usd, model, detail, scope}`; `POST /record/call` `{surface, ...}`
- `GET /limits/{provider}` -> `{blocked, resume_at}`; `POST /limits/hit` `{text, provider}` -> detected window or null; `POST /limits/success`
- `GET /quota` -> `{low, why}`
- `GET /rate?window_s=&scope=` -> `{calls, spend}`
- `POST /burn/tick` -> `{findings, brake: {personal, work}, engaged_now: [scopes]}` (Jarvis's `burn-check` alerts on `engaged_now`)
- `GET /brake/{scope}`, `POST /brake/{scope}/release`
- `POST /run/{usage-refresh|usage-digest|spend-by-effort|token-report}` (Jarvis's jobs call these)
- `GET /report`

Python callers on this Mac (Jarvis's daemon and scripts, Mind's jobs) import the `budget`
library directly (stdlib only; `~/.budget` on `sys.path` through `core.py` and `jobcommon.py`):
same code, same store, no network hop, so recording cannot fail on the service being down. The
HUD uses the service over HTTP. CLI `bin/budget` keeps the old `burn.py` CLI contract
(`record-job`, `job-budget [--optional] --scope=`; exit 0 under, 1 over, 2 could not check);
Jarvis's jobs call `bin/budget-job.sh <subcommand>` (`usage-refresh`, `spend-by-effort`).

## Failure semantics (kept from burn.py and limits.py)

- Recording never raises into the caller (the library catches; the HUD's POST is fire and
  forget, so Budget down loses that one HUD row).
- The job budget gate is three-valued: "could not check" is its own answer, and an optional
  job refuses to start on it rather than guessing.
- The brake, a limit window and quota headroom fail open (unknown = not braked, not blocked,
  not low): bookkeeping must never cause an outage. Unchanged from today.

## Cutover

One cutover, no compatibility shims: back up `jarvis.db`; copy the three tables with their rows;
move `usage/` and the limit/brake state files; switch every caller (daemon modules, handlers,
scripts, `jobcommon.py`, HUD `ledger.js`/`quota.js`/`jobvalue.js`, Mind's sleep and jobs, the
`/quota` skill); repoint the five jobs; drop the old tables only after Anze confirms. Selftest,
HUD tests and Mind tests must pass before and after.

## As built (2026-10-17)

- `budget/store.py` (paths from `BUDGET_HOME`, default `~/.budget`; tests set it to scratch),
  `ledger.py`, `brake.py`, `limits.py`, `quota.py`, `cli.py`, `jobs/` (usage refresh,
  spend-by-effort), `server/app.py` (launchd `com.budget.server`), `config.toml`, `pricing.json`
  (was `~/.jarvis/config.json`), `tests/test_budget.py`.
- Moved with their rows: `provider_calls` (86), `job_calls` (260), `usage_samples` (3,831), and
  `usage/`, `logs/limit-hits/`. The old tables are still in `jarvis.db`, unused, until Anze
  confirms dropping them; backup `~/.jarvis/jarvis.db.bak-budget`.
- Fixed on the way: brain's calls were counted twice by the burn rate and by the review ledger's
  "scoring" line (both added `brain_calls` to `provider_calls`, which already held them).
- Not moved: `run-usage-digest.py` (it posts to Notion, an outward action: Jarvis) and the
  `token-report` handler (it shells out to `~/.claude-personal/scripts/token-report.py`; moving
  it is a later, optional step).
- Jarvis keeps: `daemon/burn.py` (stuck and repeated-failure detectors, `check_keyed` combining
  them with `ledger.burn_findings`), `core.record_limit` (Budget records, Jarvis alerts), the
  brake alert in `handlers/burn_check.py`, the queue and brain refusing while braked.
